"""RecordTrades Lambda — persists the run's results.

Runs after the Map(OrderExecutor) step (live mode) or directly after
PreFlightChecks (recommend mode). Writes one row per pick to trade_log
(append-only audit) and updates pending_trades so PositionMonitor knows
what's open overnight.

Input event:
    {
      "target_T": "2026-05-26",
      "execution_mode": "recommend" | "live",
      "approved_picks": [...],            # from PreFlight
      "rejected": [...],                  # from PreFlight
      "order_results": [...],             # from Map(OrderExecutor), live only
      "preflight": {...}                  # full PreFlight output for context
    }

Return:
    {
      "trade_log_rows_written": int,
      "pending_trades_rows_written": int,
      "summary": [...]
    }

Required environment variables:
    TRADE_LOG_TABLE_NAME           CoreStack trade_log
    PENDING_TRADES_TABLE_NAME      CoreStack pending_trades
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

TRADE_LOG_TABLE = os.environ["TRADE_LOG_TABLE_NAME"]
PENDING_TRADES_TABLE = os.environ["PENDING_TRADES_TABLE_NAME"]

_dynamodb = boto3.resource("dynamodb")


def _to_decimal(v: Any) -> Any:
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    return v


def _flatten(d: dict[str, Any]) -> dict[str, Any]:
    """DDB doesn't accept nested dicts for some attrs cleanly when scanned —
    flatten contract for top-level filterability."""
    out = dict(d)
    contract = out.pop("contract", None)
    if isinstance(contract, dict):
        for k in ("strike", "right", "lastTradeDateOrContractMonth"):
            if contract.get(k) is not None and k not in out:
                out[f"contract_{k}"] = contract[k]
    return out


def _log_row(pick: dict[str, Any], status: str, target_T: str, execution_mode: str,
             order_result: dict[str, Any] | None, reason: str | None) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    trade_id = pick.get("trade_id") or f"{pick.get('ticker')}-{target_T}-{status}"
    flat = _flatten(pick)
    item: dict[str, Any] = {
        "trade_id": trade_id,
        "ts": int(now.timestamp() * 1000),
        "target_T": target_T,
        "execution_mode": execution_mode,
        "status": status,
        "logged_at": now.isoformat(),
    }
    if reason:
        item["reason"] = reason
    if order_result:
        for k in ("fill_price", "fill_qty", "ib_order_id", "submitted_at", "completed_at"):
            if order_result.get(k) is not None:
                item[k] = order_result[k]
    for k, v in flat.items():
        if k in item:
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            item[k] = _to_decimal(v)
        elif isinstance(v, list):
            item[k] = [_to_decimal(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else x for x in v]
        elif isinstance(v, dict):
            item[k] = {ik: _to_decimal(iv) if isinstance(iv, (int, float)) and not isinstance(iv, bool) else iv for ik, iv in v.items()}
        else:
            item[k] = v
    return {k: v for k, v in item.items() if v is not None}


def _open_position(pick: dict[str, Any], order_result: dict[str, Any] | None, execution_mode: str, target_T: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    contract = pick.get("contract") or {}
    item = {
        "trade_id": pick.get("trade_id"),
        "ticker": pick.get("ticker"),
        "side": pick.get("side"),
        "qty": _to_decimal((order_result or {}).get("fill_qty") if execution_mode == "live" else pick.get("qty")),
        "entry_price": _to_decimal((order_result or {}).get("fill_price") if execution_mode == "live" else pick.get("limit_price")),
        "expiry": contract.get("lastTradeDateOrContractMonth"),
        "strike": _to_decimal(contract.get("strike")),
        "right": contract.get("right"),
        "report_timing": pick.get("report_timing"),
        "earnings_date": pick.get("earnings_date"),
        "target_T": target_T,
        "execution_mode": execution_mode,
        "status": "open",
        "opened_at": now.isoformat(),
        # Hold until the NEXT market open after entry — per CLAUDE.md §2 the
        # exit is manual at the bell, but PositionMonitor uses this to time
        # the SMS alert.
        "exit_hint": "next_market_open",
    }
    return {k: v for k, v in item.items() if v is not None}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    target_T = event.get("target_T")
    execution_mode = (event.get("execution_mode") or "recommend").lower()
    approved = event.get("approved_picks") or []
    rejected = event.get("rejected") or []
    order_results = {r.get("trade_id"): r for r in (event.get("order_results") or []) if r.get("trade_id")}

    trade_log_table = _dynamodb.Table(TRADE_LOG_TABLE)
    pending_table = _dynamodb.Table(PENDING_TRADES_TABLE)
    log_written = 0
    pending_written = 0
    summary: list[dict[str, Any]] = []

    # Approved picks: in live mode the status comes from the order result; in
    # recommend mode the status is "recommended" (intended trade, no order).
    for pick in approved:
        trade_id = pick.get("trade_id")
        order_result = order_results.get(trade_id) if execution_mode == "live" else None
        if execution_mode == "live":
            status = (order_result or {}).get("status", "unknown")
        else:
            status = "recommended"
        row = _log_row(pick, status, target_T, execution_mode, order_result, reason=None)
        trade_log_table.put_item(Item=row)
        log_written += 1
        summary.append({"trade_id": trade_id, "ticker": pick.get("ticker"), "status": status})
        # Track as open only if we have a filled (or recommended) position.
        if execution_mode == "recommend" or status in ("filled", "partial"):
            pending_table.put_item(Item=_open_position(pick, order_result, execution_mode, target_T))
            pending_written += 1

    # Rejected picks: write to trade_log only (no pending row).
    for pick in rejected:
        row = _log_row(pick, "rejected", target_T, execution_mode, None, pick.get("reason"))
        trade_log_table.put_item(Item=row)
        log_written += 1
        summary.append({"trade_id": pick.get("trade_id"), "ticker": pick.get("ticker"), "status": "rejected", "reason": pick.get("reason")})

    LOG.info("RecordTrades: trade_log=%d pending=%d", log_written, pending_written)
    return {
        "trade_log_rows_written": log_written,
        "pending_trades_rows_written": pending_written,
        "summary": summary,
    }
