"""OptionsChainFetcher Lambda — per-candidate chain snapshot.

Invoked once per actionable candidate by the AfternoonEntry SFN's Map state.
For the single ticker passed in, it:
  1. Connects to IB Gateway, qualifies the underlying, gets spot
  2. Resolves the target weekly expiry (Friday after earnings)
  3. Enumerates actual calls + puts on that expiry via reqContractDetails
     (avoids the chain.strikes-is-union-across-expiries trap)
  4. Streams market data for the whole chain in batches (snapshot mode is
     incompatible with the genericTickList we need for option volume / OI /
     IV — see IBKR Warning 321)
  5. Computes aggregates:
       implied_move_pct      — from the ATM straddle
       atm_iv                — ATM implied vol
       iv_skew_25d           — 25-delta put IV minus 25-delta call IV
       put_call_volume_ratio — direction signal
       put_call_oi_ratio     — direction signal
  6. Writes the full chain to S3 as Parquet plus a sidecar .meta.json with
     the aggregates. StrategyEngine reads the sidecar at score time.

No DynamoDB write — chain data is short-lived and only relevant to today's
decision; the historical_moves table is owned by HistoricalMovesFetcher.

Input event (SFN Map per-iteration):
    {
      "ticker": "NVDA",
      "earnings_date": "2026-05-27",
      "report_timing": "amc",
      "target_T": "2026-05-26"
    }

Return value:
    {
      "ticker": "NVDA",
      "status": "ok" | "skipped",
      "reason": <only when skipped>,
      "implied_move_pct": <float>,
      "atm_strike": <float>,
      "spot": <float>,
      "s3_meta_key": "options-chains/.../NVDA.meta.json",
      "s3_parquet_key": "options-chains/.../NVDA.parquet"
    }

A `status == "skipped"` outcome RAISES `OptionsChainSkipped` so the SFN Map
iteration fails — orchestration decides whether one failure aborts the batch
or just removes that candidate from the picks.

Required environment variables:
    CONFIG_BUCKET_NAME             S3 bucket for chain snapshots
    GATEWAY_HOST                   ECS Cloud Map name (ib-gateway.astra.local)
    GATEWAY_PORT                   4004 paper / 4003 live; default 4004
    GATEWAY_CLIENT_ID              ib_async client id; default 11
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import os
import random
from datetime import date, datetime, timedelta, timezone
from typing import Any

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
from ib_async import IB, Stock, Option

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

CONFIG_BUCKET = os.environ["CONFIG_BUCKET_NAME"]
GATEWAY_HOST = os.environ["GATEWAY_HOST"]
GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "4004"))
# clientId namespace — Map runs us in parallel, so randomize per invocation
# (CLIENT_ID_BASE + 0..999) to avoid IBKR's single-client-per-id collision.
CLIENT_ID_BASE = int(os.environ.get("GATEWAY_CLIENT_ID_BASE", "2000"))

_s3 = boto3.client("s3")


class OptionsChainSkipped(Exception):
    """Raised when this candidate could not be priced. SFN Map's Catch
    converts this to a per-iteration failure so the picks list excludes it."""


# ─── small helpers ──────────────────────────────────────────────────────────


def _today_iso() -> str:
    return date.today().isoformat()


def _next_friday_after(d: date) -> date:
    days_until_fri = (4 - d.weekday()) % 7
    if days_until_fri == 0:
        days_until_fri = 7
    return d + timedelta(days=days_until_fri)


def _is_real(v: Any) -> bool:
    if v is None:
        return False
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return not math.isnan(f) and not math.isinf(f)


def _good_price(p: Any) -> bool:
    return _is_real(p) and float(p) > 0


def _safe_float(v: Any) -> float | None:
    return float(v) if _is_real(v) else None


def _safe_int(v: Any) -> int | None:
    return int(float(v)) if _is_real(v) else None


def _pick_price(*candidates: Any) -> float | None:
    for p in candidates:
        if _good_price(p):
            return float(p)
    return None


def _mid(bid: Any, ask: Any) -> float | None:
    if not (_good_price(bid) and _good_price(ask)):
        return None
    return (float(bid) + float(ask)) / 2.0


def _ticker_attr(t: Any, attr: str) -> float | None:
    return _safe_float(getattr(t, attr, None))


def _model_greek(t: Any, field: str) -> float | None:
    mg = getattr(t, "modelGreeks", None)
    if mg is None:
        return None
    return _safe_float(getattr(mg, field, None))


def _option_oi(t: Any, right: str) -> int | None:
    attr = "callOpenInterest" if right == "C" else "putOpenInterest"
    return _safe_int(getattr(t, attr, None))


# ─── IB calls ───────────────────────────────────────────────────────────────


async def _enumerate_strikes(ib: IB, ticker: str, expiry_str: str, right: str) -> list[Option]:
    template = Option(ticker, expiry_str, 0.0, right, "SMART", currency="USD")
    details = await ib.reqContractDetailsAsync(template)
    contracts = [d.contract for d in details if d.contract and d.contract.exchange]
    smart = [c for c in contracts if c.exchange == "SMART"]
    return smart or contracts


async def _fetch_chain_market_data(ib: IB, contracts: list[Option]) -> list[Any]:
    """Stream market data for the chain in batches, then cancel.

    Snapshot mode rejects genericTickList (Warning 321) — but we need ticks
    100/101/106 for option volume, OI, IV. Stream instead, batched under the
    ~100 simultaneous market-data-line cap."""
    BATCH_SIZE = 90
    WAIT_SECONDS = 6
    tickers: list[Any] = [None] * len(contracts)
    for start in range(0, len(contracts), BATCH_SIZE):
        batch = contracts[start:start + BATCH_SIZE]
        batch_tickers: list[Any] = []
        for c in batch:
            t = ib.reqMktData(c, "100,101,106", snapshot=False, regulatorySnapshot=False)
            batch_tickers.append(t)
        await asyncio.sleep(WAIT_SECONDS)
        for i, c in enumerate(batch):
            ib.cancelMktData(c)
            tickers[start + i] = batch_tickers[i]
    return tickers


# ─── per-candidate processing ───────────────────────────────────────────────


async def _process_candidate(ib: IB, ticker: str, earnings_date: str) -> dict[str, Any]:
    stock = Stock(ticker, "SMART", "USD")
    await ib.qualifyContractsAsync(stock)
    if not stock.conId:
        raise OptionsChainSkipped(f"[{ticker}] failed to qualify underlying")

    spot_ticker = ib.reqMktData(stock, "", snapshot=True, regulatorySnapshot=False)
    await asyncio.sleep(2)
    spot = _pick_price(spot_ticker.marketPrice(), spot_ticker.last, spot_ticker.close)
    ib.cancelMktData(stock)
    if spot is None:
        raise OptionsChainSkipped(f"[{ticker}] no usable spot price")

    chains = await ib.reqSecDefOptParamsAsync(stock.symbol, "", stock.secType, stock.conId)
    chain = next((c for c in chains if c.exchange == "SMART"), chains[0] if chains else None)
    if chain is None:
        raise OptionsChainSkipped(f"[{ticker}] no option chain returned")
    target_expiry = _next_friday_after(date.fromisoformat(earnings_date))
    target_expiry_str = target_expiry.strftime("%Y%m%d")
    if target_expiry_str not in chain.expirations:
        future = sorted(e for e in chain.expirations if e >= target_expiry_str)
        if not future:
            raise OptionsChainSkipped(f"[{ticker}] no future expiries available")
        target_expiry_str = future[0]
        LOG.info("[%s] target expiry %s unavailable; using %s",
                 ticker, target_expiry.strftime("%Y%m%d"), target_expiry_str)

    calls = await _enumerate_strikes(ib, ticker, target_expiry_str, "C")
    puts = await _enumerate_strikes(ib, ticker, target_expiry_str, "P")
    all_contracts = calls + puts
    if not all_contracts:
        raise OptionsChainSkipped(f"[{ticker}] no contracts on expiry {target_expiry_str}")
    LOG.info("[%s] expiry %s: %d calls + %d puts",
             ticker, target_expiry_str, len(calls), len(puts))

    tickers = await _fetch_chain_market_data(ib, all_contracts)

    chain_rows: list[dict[str, Any]] = []
    for c, t in zip(all_contracts, tickers):
        bid = _ticker_attr(t, "bid")
        ask = _ticker_attr(t, "ask")
        last = _ticker_attr(t, "last")
        close = _ticker_attr(t, "close")
        # Prefer bid-ask mid when the book is alive (market open). Fall back
        # to last trade then close so closed-market / frozen data still
        # yields a usable price for ATM selection.
        mid = _mid(bid, ask) or _pick_price(last, close)
        chain_rows.append({
            "strike": float(c.strike),
            "right": c.right,
            "bid": bid,
            "ask": ask,
            "last": last,
            "close": close,
            "bid_size": _safe_int(getattr(t, "bidSize", None)),
            "ask_size": _safe_int(getattr(t, "askSize", None)),
            "mid": mid,
            "moneyness": float(c.strike) / spot,
            "iv": _model_greek(t, "impliedVol"),
            "delta": _model_greek(t, "delta"),
            "gamma": _model_greek(t, "gamma"),
            "theta": _model_greek(t, "theta"),
            "vega": _model_greek(t, "vega"),
            "volume": _safe_int(getattr(t, "volume", None)),
            "open_interest": _option_oi(t, c.right),
        })

    calls_with_mid = [r for r in chain_rows if r["right"] == "C" and r["mid"] is not None]
    puts_with_mid = [r for r in chain_rows if r["right"] == "P" and r["mid"] is not None]
    atm_call = min(calls_with_mid, key=lambda r: abs(r["strike"] - spot)) if calls_with_mid else None
    atm_put = min(puts_with_mid, key=lambda r: abs(r["strike"] - spot)) if puts_with_mid else None
    if not (atm_call and atm_put):
        raise OptionsChainSkipped(f"[{ticker}] missing ATM call/put with mid prices")

    implied_move_pct = (atm_call["mid"] + atm_put["mid"]) / spot
    if not _good_price(implied_move_pct):
        raise OptionsChainSkipped(f"[{ticker}] implied_move_pct invalid")

    atm_iv = atm_call["iv"] if atm_call["iv"] is not None else atm_put["iv"]

    call_25d = min(
        (r for r in chain_rows if r["right"] == "C" and r["delta"] is not None),
        key=lambda r: abs(r["delta"] - 0.25), default=None,
    )
    put_25d = min(
        (r for r in chain_rows if r["right"] == "P" and r["delta"] is not None),
        key=lambda r: abs(r["delta"] - (-0.25)), default=None,
    )
    iv_skew_25d = None
    if call_25d and put_25d and call_25d["iv"] is not None and put_25d["iv"] is not None:
        iv_skew_25d = put_25d["iv"] - call_25d["iv"]

    call_vol = sum(r["volume"] or 0 for r in chain_rows if r["right"] == "C")
    put_vol = sum(r["volume"] or 0 for r in chain_rows if r["right"] == "P")
    call_oi = sum(r["open_interest"] or 0 for r in chain_rows if r["right"] == "C")
    put_oi = sum(r["open_interest"] or 0 for r in chain_rows if r["right"] == "P")
    pc_vol_ratio = (put_vol / call_vol) if call_vol > 0 else None
    pc_oi_ratio = (put_oi / call_oi) if call_oi > 0 else None

    LOG.info(
        "[%s] implied=%.4f atm_iv=%s pcvol=%s pcoi=%s skew=%s",
        ticker, implied_move_pct,
        f"{atm_iv:.4f}" if atm_iv is not None else "n/a",
        f"{pc_vol_ratio:.3f}" if pc_vol_ratio is not None else "n/a",
        f"{pc_oi_ratio:.3f}" if pc_oi_ratio is not None else "n/a",
        f"{iv_skew_25d:.4f}" if iv_skew_25d is not None else "n/a",
    )

    today = _today_iso()
    parquet_key = f"options-chains/{today}/{ticker}.parquet"
    meta_key = f"options-chains/{today}/{ticker}.meta.json"

    pa_table = pa.Table.from_pylist(chain_rows)
    buf = io.BytesIO()
    pq.write_table(pa_table, buf, compression="snappy")
    buf.seek(0)
    _s3.put_object(
        Bucket=CONFIG_BUCKET, Key=parquet_key, Body=buf.read(),
        ContentType="application/octet-stream",
    )

    meta = {
        "ticker": ticker,
        "earnings_date": earnings_date,
        "expiry": target_expiry_str,
        "spot": spot,
        "atm_strike": atm_call["strike"],
        "atm_call_mid": atm_call["mid"],
        "atm_put_mid": atm_put["mid"],
        "atm_iv": atm_iv,
        "implied_move_pct": implied_move_pct,
        "iv_skew_25d": iv_skew_25d,
        "put_call_volume_ratio": pc_vol_ratio,
        "put_call_oi_ratio": pc_oi_ratio,
        "call_volume_total": call_vol,
        "put_volume_total": put_vol,
        "call_oi_total": call_oi,
        "put_oi_total": put_oi,
        "atm_call_bid": atm_call["bid"],
        "atm_call_ask": atm_call["ask"],
        "atm_put_bid": atm_put["bid"],
        "atm_put_ask": atm_put["ask"],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _s3.put_object(
        Bucket=CONFIG_BUCKET, Key=meta_key,
        Body=json.dumps(meta, default=str).encode(),
        ContentType="application/json",
    )

    return {
        "ticker": ticker,
        "status": "ok",
        "spot": spot,
        "atm_strike": atm_call["strike"],
        "implied_move_pct": implied_move_pct,
        "atm_iv": atm_iv,
        "s3_meta_key": meta_key,
        "s3_parquet_key": parquet_key,
    }


# ─── orchestration ──────────────────────────────────────────────────────────


async def _run(ticker: str, earnings_date: str) -> dict[str, Any]:
    ib = IB()
    client_id = CLIENT_ID_BASE + random.randint(0, 999)
    LOG.info("Connecting to IB Gateway at %s:%d (clientId=%d)", GATEWAY_HOST, GATEWAY_PORT, client_id)
    await ib.connectAsync(GATEWAY_HOST, GATEWAY_PORT, clientId=client_id, readonly=True)
    LOG.info("Connected; managed accounts: %s", ib.managedAccounts())
    # Delayed-frozen: returns last-known delayed prices even when the market
    # is closed. Live OPRA subscription still serves live when active.
    ib.reqMarketDataType(4)
    try:
        return await _process_candidate(ib, ticker, earnings_date)
    finally:
        ib.disconnect()


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """SFN Map per-iteration entry.

    Expects {ticker, earnings_date, ...}. Raises OptionsChainSkipped (which
    fails the Lambda invocation) if the candidate cannot be priced — SFN's
    Map.Catch handles the partial failure."""
    ticker = event.get("ticker")
    earnings_date = event.get("earnings_date")
    if not (ticker and earnings_date):
        raise ValueError(f"Missing ticker / earnings_date in event: {event!r}")
    return asyncio.run(_run(ticker, earnings_date))
