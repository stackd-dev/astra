"""HistoricalMovesFetcher Lambda — per-ticker nightly job.

For one ticker, takes the past N earnings dates (supplied in the event
payload by EarningsFetcher — we have no internet egress from inside the
VPC, so we can't call Finnhub ourselves) and pulls the underlying's
prior-close / next-open around each via IBKR reqHistoricalData. Writes
one row per past quarter to historical_moves.

Why split from OptionsChainFetcher: historical moves are slow-moving (only
change after each new earnings) so they belong in the nightly cycle, while
the option chain is fast-moving (drifts intraday) and must be re-fetched
in the afternoon right before the entry decision. StrategyEngine joins
both at score time.

Input event (SFN Map per-iteration):
    {
      "ticker": "NVDA",
      "earnings_date": "2026-05-27",
      "past_earnings_dates": ["2026-02-25", "2025-11-19", ...],
      ...
    }

Return value:
    {
      "ticker": "NVDA",
      "status": "ok",
      "quarters_written": <int>,
      "mean_actual_move_pct": <float>
    }

Raises HistoricalMovesUnavailable on hard failure (no quarters retrievable)
so SFN Map.Catch can decide whether to drop this candidate.

Required environment variables:
    HISTORICAL_MOVES_TABLE_NAME    CoreStack historical_moves table
    GATEWAY_HOST                   ECS Cloud Map (ib-gateway.astra.local)
    GATEWAY_PORT                   4004 paper / 4003 live
    GATEWAY_CLIENT_ID_BASE         base for randomized clientId; default 1000
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import random
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from statistics import mean
from typing import Any

import boto3
from ib_async import IB, Stock

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

HIST_TABLE = os.environ["HISTORICAL_MOVES_TABLE_NAME"]
GATEWAY_HOST = os.environ["GATEWAY_HOST"]
GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "4004"))
# clientId namespace for this Lambda family. SFN Map runs us in parallel, so
# pick a fresh id per invocation to avoid IBKR's "one client per id" hang —
# the env var is the BASE, runtime adds a random offset (0..999).
CLIENT_ID_BASE = int(os.environ.get("GATEWAY_CLIENT_ID_BASE", "1000"))

_dynamodb = boto3.resource("dynamodb")


class HistoricalMovesUnavailable(Exception):
    pass


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


def _to_decimal(v: Any) -> Any:
    if v is None or not _is_real(v):
        return None
    return Decimal(str(v))


async def _actual_move(ib: IB, stock: Stock, earnings_date: date) -> tuple[float, float, float] | None:
    """Return (prior_close, next_open, abs_pct_move) around the earnings date."""
    end = earnings_date + timedelta(days=3)
    end_str = end.strftime("%Y%m%d") + " 23:59:59 US/Eastern"
    try:
        bars = await ib.reqHistoricalDataAsync(
            stock,
            endDateTime=end_str,
            durationStr="7 D",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
    except Exception as e:  # noqa: BLE001
        LOG.warning("[%s] hist bars failed @ %s: %s", stock.symbol, earnings_date, e)
        return None
    if not bars:
        return None

    prior_close: float | None = None
    next_open: float | None = None
    for bar in bars:
        bd = bar.date if isinstance(bar.date, date) else date.fromisoformat(str(bar.date))
        if bd < earnings_date and _good_price(bar.close):
            prior_close = float(bar.close)
        elif bd > earnings_date and next_open is None and _good_price(bar.open):
            next_open = float(bar.open)
            break
    if prior_close is None or next_open is None:
        return None
    return prior_close, next_open, abs(next_open - prior_close) / prior_close


def _quarter_key(d: date) -> str:
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


async def _run(ticker: str, past_dates: list[date]) -> dict[str, Any]:
    stock = Stock(ticker, "SMART", "USD")
    ib = IB()
    client_id = CLIENT_ID_BASE + random.randint(0, 999)
    LOG.info("[%s] connecting to IB Gateway %s:%d (clientId=%d)",
             ticker, GATEWAY_HOST, GATEWAY_PORT, client_id)
    await ib.connectAsync(GATEWAY_HOST, GATEWAY_PORT, clientId=client_id, readonly=True)
    ib.reqMarketDataType(4)
    try:
        await ib.qualifyContractsAsync(stock)
        if not stock.conId:
            raise HistoricalMovesUnavailable(f"[{ticker}] underlying did not qualify")

        table = _dynamodb.Table(HIST_TABLE)
        moves: list[float] = []
        quarters_written = 0
        with table.batch_writer() as batch:
            for ed in past_dates:
                res = await _actual_move(ib, stock, ed)
                if res is None:
                    continue
                prior_close, next_open, pct = res
                moves.append(pct)
                quarters_written += 1
                batch.put_item(Item={
                    "ticker": ticker,
                    "quarter": _quarter_key(ed),
                    "earnings_date": ed.isoformat(),
                    "prior_close": _to_decimal(prior_close),
                    "next_open": _to_decimal(next_open),
                    "actual_move_pct": _to_decimal(pct),
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                })
        if quarters_written == 0:
            raise HistoricalMovesUnavailable(f"[{ticker}] no quarters had usable bars")
        mean_move = mean(moves)
        LOG.info("[%s] wrote %d quarters; mean abs move = %.4f", ticker, quarters_written, mean_move)
        return {
            "ticker": ticker,
            "status": "ok",
            "quarters_written": quarters_written,
            "mean_actual_move_pct": mean_move,
        }
    finally:
        ib.disconnect()


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    ticker = event.get("ticker")
    if not ticker:
        raise ValueError(f"Missing ticker in event: {event!r}")
    raw_dates = event.get("past_earnings_dates") or []
    past_dates: list[date] = []
    for s in raw_dates:
        try:
            past_dates.append(date.fromisoformat(s))
        except (TypeError, ValueError):
            LOG.warning("[%s] skipping unparseable past date: %r", ticker, s)
    if not past_dates:
        raise HistoricalMovesUnavailable(
            f"[{ticker}] no past_earnings_dates in event — EarningsFetcher should supply them"
        )
    return asyncio.run(_run(ticker, past_dates))
