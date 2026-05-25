"""OptionsChainFetcher Lambda — for each actionable candidate from
earnings_calendar, pull the options chain from IB Gateway and compute the
implied move from the ATM straddle. Writes implied_move to historical_moves
and snapshots the chain to S3.

v1 scope: implied_move only. The edge_ratio (mean of last ~8 quarters of
actual post-earnings moves / implied_move) per design-doc §3.2 is deferred —
needs a historical earnings-dates data source (Finnhub free tier is
forward-only; user prefers no yfinance). StrategyEngine should treat
missing edge_ratio as 1.0 (neutral) until backfilled.

Required environment variables:
    EARNINGS_CALENDAR_TABLE_NAME   CoreStack earnings_calendar table
    HISTORICAL_MOVES_TABLE_NAME    CoreStack historical_moves table
    CONFIG_BUCKET_NAME             CoreStack S3 bucket (chain snapshots go to
                                   options-chains/<date>/<ticker>.json)
    GATEWAY_HOST                   ECS Cloud Map name, e.g. ib-gateway.astra.local
    GATEWAY_PORT                   4004 (paper) or 4003 (live); default 4004
    GATEWAY_CLIENT_ID              Distinct ib_async client id; default 10
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import boto3
from ib_async import IB, Stock, Option

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

EARNINGS_TABLE = os.environ["EARNINGS_CALENDAR_TABLE_NAME"]
HIST_MOVES_TABLE = os.environ["HISTORICAL_MOVES_TABLE_NAME"]
CONFIG_BUCKET = os.environ["CONFIG_BUCKET_NAME"]
GATEWAY_HOST = os.environ["GATEWAY_HOST"]
GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "4004"))
CLIENT_ID = int(os.environ.get("GATEWAY_CLIENT_ID", "10"))

_dynamodb = boto3.resource("dynamodb")
_s3 = boto3.client("s3")


def _to_decimal(v: Any) -> Any:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return Decimal(str(v))
    return v


def _today_iso() -> str:
    return date.today().isoformat()


def _next_friday_after(d: date) -> date:
    """Pick the weekly expiration: Friday strictly after d. If d IS a Friday,
    use the following Friday (we want post-earnings expiry, not same-day)."""
    days_until_fri = (4 - d.weekday()) % 7
    if days_until_fri == 0:
        days_until_fri = 7
    return d + timedelta(days=days_until_fri)


def _get_actionable_candidates() -> list[dict[str, Any]]:
    """Read today's actionable rows from earnings_calendar (target_T == today)."""
    table = _dynamodb.Table(EARNINGS_TABLE)
    today = _today_iso()
    # Scan + filter is fine — we expect well under 100 rows total.
    resp = table.scan(
        FilterExpression="target_T = :t",
        ExpressionAttributeValues={":t": today},
    )
    items = resp.get("Items", [])
    LOG.info("Found %d actionable candidates for target_T=%s", len(items), today)
    return items


def _good_price(p: Any) -> bool:
    """True if p is a real positive number — not None, not NaN, > 0.
    NaN comparisons in Python always return False, so without isnan() a NaN
    silently propagates through (NaN <= 0 is False, NaN > 0 is False)."""
    return p is not None and isinstance(p, (int, float)) and not math.isnan(p) and p > 0


def _pick_price(*candidates: Any) -> float | None:
    """First usable price from a list of candidates; NaN-safe.
    Replaces `marketPrice() or last or close` which is broken because
    NaN is truthy in Python `or`."""
    for p in candidates:
        if _good_price(p):
            return float(p)
    return None


def _mid(bid: float | None, ask: float | None) -> float | None:
    if not (_good_price(bid) and _good_price(ask)):
        return None
    return (bid + ask) / 2.0


async def _fetch_one(ib: IB, ticker: str, earnings_date: str) -> dict[str, Any] | None:
    """Pull ATM straddle for one ticker; compute implied move. Returns a
    summary dict, or None if the chain isn't usable (no weekly, no liquidity,
    no contract details, etc.)."""
    try:
        stock = Stock(ticker, "SMART", "USD")
        await ib.qualifyContractsAsync(stock)
        if not stock.conId:
            LOG.warning("[%s] failed to qualify underlying", ticker)
            return None

        # Get spot price — request market data, wait briefly for first tick.
        # NaN-safe: marketPrice() etc. return NaN when no data, which is
        # truthy in Python `or` and would silently propagate through math.
        spot_ticker = ib.reqMktData(stock, "", snapshot=True, regulatorySnapshot=False)
        await asyncio.sleep(2)
        spot = _pick_price(spot_ticker.marketPrice(), spot_ticker.last, spot_ticker.close)
        ib.cancelMktData(stock)
        if spot is None:
            LOG.warning("[%s] no usable spot price (live/last/close all NaN)", ticker)
            return None

        # Get the chain structure to find available expirations + strikes.
        chains = await ib.reqSecDefOptParamsAsync(
            stock.symbol, "", stock.secType, stock.conId
        )
        # Prefer SMART exchange entry; fall back to first.
        chain = next((c for c in chains if c.exchange == "SMART"), chains[0] if chains else None)
        if chain is None:
            LOG.warning("[%s] no option chain returned", ticker)
            return None

        # Pick the weekly expiration: the Friday strictly after the earnings date.
        target_expiry = _next_friday_after(date.fromisoformat(earnings_date))
        target_expiry_str = target_expiry.strftime("%Y%m%d")
        if target_expiry_str not in chain.expirations:
            # Fall back to the nearest available expiry on or after target.
            future = sorted(e for e in chain.expirations if e >= target_expiry_str)
            if not future:
                LOG.warning("[%s] no future expiries available", ticker)
                return None
            target_expiry_str = future[0]

        # Find the ATM strike (closest to spot).
        atm_strike = min(chain.strikes, key=lambda s: abs(s - spot))

        # Build the call + put contracts at ATM. Don't pass tradingClass —
        # the chain's reported tradingClass on SMART is sometimes a weekly
        # variant (e.g. "2MSFT") that doesn't match the strike; let
        # qualifyContracts resolve the right class for this strike.
        call = Option(ticker, target_expiry_str, atm_strike, "C", "SMART")
        put = Option(ticker, target_expiry_str, atm_strike, "P", "SMART")
        await ib.qualifyContractsAsync(call, put)
        if not (call.conId and put.conId):
            LOG.warning(
                "[%s] ATM straddle didn't qualify — strike=%s expiry=%s likely doesn't exist",
                ticker, atm_strike, target_expiry_str,
            )
            return None

        # Snapshot quotes for both.
        call_t = ib.reqMktData(call, "", snapshot=True, regulatorySnapshot=False)
        put_t = ib.reqMktData(put, "", snapshot=True, regulatorySnapshot=False)
        await asyncio.sleep(2)
        call_mid = _mid(call_t.bid, call_t.ask)
        put_mid = _mid(put_t.bid, put_t.ask)
        ib.cancelMktData(call)
        ib.cancelMktData(put)

        if call_mid is None or put_mid is None:
            LOG.warning("[%s] missing bid/ask on ATM straddle (no subscription / market closed)", ticker)
            return None

        implied_move_pct = (call_mid + put_mid) / spot
        # Final NaN guard — even if individual mids passed, the result could
        # be Inf/NaN from edge cases. DynamoDB rejects both.
        if not _good_price(implied_move_pct):
            LOG.warning("[%s] implied_move_pct not a real number: %s", ticker, implied_move_pct)
            return None

        return {
            "ticker": ticker,
            "earnings_date": earnings_date,
            "expiry": target_expiry_str,
            "spot": spot,
            "atm_strike": atm_strike,
            "call_mid": call_mid,
            "put_mid": put_mid,
            "implied_move_pct": implied_move_pct,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:  # noqa: BLE001 — per-ticker errors must not abort the batch
        LOG.exception("[%s] chain fetch failed: %s", ticker, e)
        return None


def _write_historical_move(result: dict[str, Any]) -> None:
    """Write implied_move row to historical_moves. quarter = YYYYQn from
    earnings_date so we get one row per ticker per quarter (upsert)."""
    table = _dynamodb.Table(HIST_MOVES_TABLE)
    ed = date.fromisoformat(result["earnings_date"])
    quarter_key = f"{ed.year}Q{(ed.month - 1) // 3 + 1}"
    item = {
        "ticker": result["ticker"],
        "quarter": quarter_key,
        "earnings_date": result["earnings_date"],
        "expiry": result["expiry"],
        "spot": _to_decimal(result["spot"]),
        "atm_strike": _to_decimal(result["atm_strike"]),
        "call_mid": _to_decimal(result["call_mid"]),
        "put_mid": _to_decimal(result["put_mid"]),
        "implied_move_pct": _to_decimal(result["implied_move_pct"]),
        # edge_ratio deferred — needs historical earnings-dates source
        "actual_move_pct": None,
        "edge_ratio": None,
        "fetched_at": result["fetched_at"],
    }
    item = {k: v for k, v in item.items() if v is not None}
    table.put_item(Item=item)


def _snapshot_chain(result: dict[str, Any]) -> None:
    """Write a JSON snapshot of the ATM straddle data to S3 per design-doc §3.2
    (Parquet planned for later; JSON for v1 simplicity)."""
    key = f"options-chains/{_today_iso()}/{result['ticker']}.json"
    _s3.put_object(
        Bucket=CONFIG_BUCKET,
        Key=key,
        Body=json.dumps(result, default=str).encode(),
        ContentType="application/json",
    )


async def _run(candidates: list[dict[str, Any]]) -> dict[str, int]:
    ib = IB()
    LOG.info("Connecting to IB Gateway at %s:%d (clientId=%d)", GATEWAY_HOST, GATEWAY_PORT, CLIENT_ID)
    await ib.connectAsync(GATEWAY_HOST, GATEWAY_PORT, clientId=CLIENT_ID, readonly=True)
    LOG.info("Connected; managed accounts: %s", ib.managedAccounts())

    # Use delayed market data (15-min lag, free) since we don't have an OPRA
    # real-time subscription. Without this, all reqMktData calls return NaN
    # and Error 10089. Switch to type 1 (live) when subscribed; see CLAUDE.md §14.
    ib.reqMarketDataType(3)

    fetched = 0
    written = 0
    skipped = 0
    try:
        for cand in candidates:
            ticker = cand.get("ticker")
            earnings_date = cand.get("date")
            if not (ticker and earnings_date):
                skipped += 1
                continue
            result = await _fetch_one(ib, ticker, earnings_date)
            if result is None:
                skipped += 1
                continue
            fetched += 1
            try:
                _write_historical_move(result)
                _snapshot_chain(result)
                written += 1
            except Exception:  # noqa: BLE001
                LOG.exception("[%s] write failed", ticker)
    finally:
        ib.disconnect()

    return {"fetched": fetched, "written": written, "skipped": skipped}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    candidates = _get_actionable_candidates()
    if not candidates:
        return {"target_T": _today_iso(), "candidates": 0, "fetched": 0, "written": 0, "skipped": 0}

    stats = asyncio.run(_run(candidates))
    LOG.info(
        "OptionsChainFetcher complete: candidates=%d fetched=%d written=%d skipped=%d",
        len(candidates), stats["fetched"], stats["written"], stats["skipped"],
    )
    return {"target_T": _today_iso(), "candidates": len(candidates), **stats}
