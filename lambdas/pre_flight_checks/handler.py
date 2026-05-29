"""PreFlightChecks Lambda — hard safety rails before any order leaves the
SFN. Per CLAUDE.md §10:

  * STOP file in the S3 config bucket short-circuits everything.
  * Max N trades/day enforced against trade_log.
  * Max $/day deployed enforced against trade_log.
  * Per-ticker position cap enforced against pending_trades.
  * Reject any qty > 2 × avg historical fill size (sanity).
  * Time gate: no orders sent after 3:55pm ET (the executor would otherwise
    fail mid-flight against IBKR's hard 4pm close).

Recommend-only vs live mode is read here and propagated to the downstream
Choice state.

Input event (from StrategyEngine):
    {
      "target_T": "...",
      "picks": [...],
      ...
    }

Return:
    {
      "target_T": "...",
      "approved_picks": [...],
      "rejected": [{ticker, side, reason}, ...],
      "execution_mode": "recommend" | "live",
      "stop_file_active": bool,
      "preflight_at": <iso>
    }

Required environment variables:
    CONFIG_BUCKET_NAME            S3 bucket (looks up STOP file + execution-mode.json)
    TRADE_LOG_TABLE_NAME          DDB (for daily cap enforcement)
    PENDING_TRADES_TABLE_NAME     DDB (for per-ticker position cap)
    STOP_FILE_KEY                 default "STOP"
    EXECUTION_MODE_KEY            default "execution-mode.json"
    MAX_TRADES_PER_DAY            default "10"
    MAX_USD_PER_DAY               default "10000"
    MAX_POSITIONS_PER_TICKER      default "1"
    LATEST_ENTRY_TIME_ET          default "15:55"
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import boto3
from boto3.dynamodb.conditions import Attr

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

CONFIG_BUCKET = os.environ["CONFIG_BUCKET_NAME"]
TRADE_LOG_TABLE = os.environ["TRADE_LOG_TABLE_NAME"]
PENDING_TRADES_TABLE = os.environ["PENDING_TRADES_TABLE_NAME"]
STOP_FILE_KEY = os.environ.get("STOP_FILE_KEY", "STOP")
EXECUTION_MODE_KEY = os.environ.get("EXECUTION_MODE_KEY", "execution-mode.json")
MAX_TRADES_PER_DAY = int(os.environ.get("MAX_TRADES_PER_DAY", "10"))
MAX_USD_PER_DAY = float(os.environ.get("MAX_USD_PER_DAY", "10000"))
MAX_POSITIONS_PER_TICKER = int(os.environ.get("MAX_POSITIONS_PER_TICKER", "1"))
LATEST_ENTRY_TIME_ET = os.environ.get("LATEST_ENTRY_TIME_ET", "15:55")

ET = ZoneInfo("America/New_York")
_s3 = boto3.client("s3")
_dynamodb = boto3.resource("dynamodb")


def _f(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    return float(v)


def _stop_file_active() -> bool:
    try:
        _s3.head_object(Bucket=CONFIG_BUCKET, Key=STOP_FILE_KEY)
        return True
    except _s3.exceptions.ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def _read_execution_mode() -> str:
    try:
        obj = _s3.get_object(Bucket=CONFIG_BUCKET, Key=EXECUTION_MODE_KEY)
        body = json.loads(obj["Body"].read())
        mode = body.get("mode", "recommend").lower()
        return mode if mode in ("recommend", "live") else "recommend"
    except _s3.exceptions.NoSuchKey:
        # Default = recommend per CLAUDE.md §12: validation gate before live.
        return "recommend"
    except Exception:  # noqa: BLE001
        LOG.exception("Failed to read execution-mode.json; defaulting to recommend")
        return "recommend"


def _todays_trade_log() -> list[dict[str, Any]]:
    """Today's trade_log entries (the cap is per-day)."""
    table = _dynamodb.Table(TRADE_LOG_TABLE)
    today_iso = date.today().isoformat()
    resp = table.scan(
        FilterExpression=Attr("target_T").eq(today_iso) & Attr("status").is_in(["live", "filled", "partial"])
    )
    return resp.get("Items", [])


def _open_positions_by_ticker() -> dict[str, int]:
    table = _dynamodb.Table(PENDING_TRADES_TABLE)
    resp = table.scan(FilterExpression=Attr("status").eq("open"))
    counts: dict[str, int] = {}
    for item in resp.get("Items", []):
        t = item.get("ticker")
        if t:
            counts[t] = counts.get(t, 0) + 1
    return counts


def _ticker_avg_fill_size(ticker: str) -> float | None:
    """Mean qty across all prior filled rows for this ticker in trade_log."""
    table = _dynamodb.Table(TRADE_LOG_TABLE)
    resp = table.scan(
        FilterExpression=Attr("ticker").eq(ticker) & Attr("status").is_in(["filled", "partial"])
    )
    items = resp.get("Items", [])
    qtys = [_f(i.get("qty")) for i in items if _f(i.get("qty"))]
    if not qtys:
        return None
    return sum(qtys) / len(qtys)


def _past_time_gate(now_et: datetime) -> bool:
    hh, mm = LATEST_ENTRY_TIME_ET.split(":")
    cutoff = time(int(hh), int(mm))
    return now_et.timetz().replace(tzinfo=None) > cutoff


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    target_T = event.get("target_T") or date.today().isoformat()
    picks = event.get("picks") or []
    now_et = datetime.now(ET)

    execution_mode = _read_execution_mode()
    stop_active = _stop_file_active()
    past_cutoff = _past_time_gate(now_et)

    LOG.info(
        "PreFlight: target_T=%s picks=%d mode=%s stop=%s past_cutoff=%s now_et=%s",
        target_T, len(picks), execution_mode, stop_active, past_cutoff, now_et.isoformat(),
    )

    # Hard short-circuits — every pick rejected, no orders attempted.
    if stop_active or past_cutoff:
        reason = "STOP_FILE_ACTIVE" if stop_active else "PAST_ENTRY_CUTOFF"
        return {
            "target_T": target_T,
            "approved_picks": [],
            "rejected": [{**p, "reason": reason} for p in picks],
            "execution_mode": execution_mode,
            "stop_file_active": stop_active,
            "past_cutoff": past_cutoff,
            "preflight_at": datetime.now(ET).isoformat(),
        }

    todays_log = _todays_trade_log()
    trades_today = len(todays_log)
    usd_today = sum((_f(i.get("qty")) or 0) * (_f(i.get("limit_price")) or 0) * 100 for i in todays_log)
    open_by_ticker = _open_positions_by_ticker()

    approved: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    remaining_trade_budget = MAX_TRADES_PER_DAY - trades_today
    remaining_usd_budget = MAX_USD_PER_DAY - usd_today

    for pick in picks:
        ticker = pick.get("ticker", "?")
        qty = _f(pick.get("qty")) or 0
        limit = _f(pick.get("limit_price")) or 0
        cost = qty * limit * 100

        if remaining_trade_budget <= 0:
            rejected.append({**pick, "reason": "MAX_TRADES_PER_DAY_REACHED"})
            continue
        if cost > remaining_usd_budget:
            rejected.append({**pick, "reason": f"WOULD_EXCEED_DAILY_USD_CAP ({cost:.0f} > {remaining_usd_budget:.0f})"})
            continue
        if open_by_ticker.get(ticker, 0) >= MAX_POSITIONS_PER_TICKER:
            rejected.append({**pick, "reason": "TICKER_POSITION_CAP_REACHED"})
            continue
        avg = _ticker_avg_fill_size(ticker)
        if avg is not None and qty > 2 * avg:
            rejected.append({**pick, "reason": f"QTY_{qty}_EXCEEDS_2X_AVG_{avg:.1f}"})
            continue

        approved.append(pick)
        remaining_trade_budget -= 1
        remaining_usd_budget -= cost
        open_by_ticker[ticker] = open_by_ticker.get(ticker, 0) + 1

    LOG.info("PreFlight: approved=%d rejected=%d", len(approved), len(rejected))
    return {
        "target_T": target_T,
        "approved_picks": approved,
        "rejected": rejected,
        "execution_mode": execution_mode,
        "stop_file_active": stop_active,
        "past_cutoff": past_cutoff,
        "trades_today": trades_today,
        "usd_today": usd_today,
        "preflight_at": datetime.now(ET).isoformat(),
    }
