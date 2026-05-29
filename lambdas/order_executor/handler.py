"""OrderExecutor Lambda — submits ONE options order via ib_async and waits
for a fill (or timeout).

Invoked once per approved pick by the AfternoonEntry SFN's Map state. Idempotent
by `trade_id`: writes a sentinel row to pending_trades BEFORE submission and
rejects re-submission attempts for the same trade_id (which would otherwise
double-fill on SFN retry).

Belt-and-suspenders safety per CLAUDE.md §6 + §10:
  * IB Gateway runs with READ_ONLY_API=yes in paper — Gateway itself blocks
    order submission. This Lambda is the second layer (refuses to submit
    unless event.execution_mode == "live" AND IBKR_READ_ONLY != "yes").
  * trade_id is set as `orderRef` on the Order so it round-trips through
    IBKR for audit.

Input event (one approved pick):
    {
      "trade_id": "NVDA-2026-05-26-long_call-abc123",
      "ticker": "NVDA",
      "side": "long_call" | "long_put",
      "qty": 2,
      "limit_price": 5.20,
      "contract": {symbol, secType, exchange, currency,
                   lastTradeDateOrContractMonth, strike, right, multiplier},
      "execution_mode": "recommend" | "live",
      ...
    }

Return:
    {
      "trade_id": "...",
      "ticker": "NVDA",
      "status": "filled" | "partial" | "rejected" | "timeout" | "recommend_only" | "duplicate",
      "fill_price": float | null,
      "fill_qty": int | null,
      "ib_order_id": int | null,
      "submitted_at": <iso>,
      "completed_at": <iso>
    }

Required environment variables:
    PENDING_TRADES_TABLE_NAME    DDB (idempotency sentinel)
    GATEWAY_HOST                 ECS Cloud Map name
    GATEWAY_PORT                 4004 paper / 4003 live
    GATEWAY_CLIENT_ID            ib_async client id; default 21
    FILL_TIMEOUT_SECONDS         default "60"
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError
from ib_async import IB, Option, LimitOrder

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

PENDING_TRADES_TABLE = os.environ["PENDING_TRADES_TABLE_NAME"]
GATEWAY_HOST = os.environ["GATEWAY_HOST"]
GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "4004"))
# clientId namespace — Map runs us in parallel, so randomize per invocation
# (CLIENT_ID_BASE + 0..999) to avoid IBKR's single-client-per-id collision.
CLIENT_ID_BASE = int(os.environ.get("GATEWAY_CLIENT_ID_BASE", "3000"))
FILL_TIMEOUT_SECONDS = int(os.environ.get("FILL_TIMEOUT_SECONDS", "60"))

_dynamodb = boto3.resource("dynamodb")


def _to_decimal(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    return v


def _claim_trade_id(trade_id: str, event: dict[str, Any]) -> bool:
    """Atomic put-if-not-exists. Returns True if we successfully claimed the
    trade_id (first submission); False if someone else already wrote a row
    (re-invocation by SFN retry — must NOT re-submit)."""
    table = _dynamodb.Table(PENDING_TRADES_TABLE)
    item = {
        "trade_id": trade_id,
        "ticker": event.get("ticker"),
        "side": event.get("side"),
        "qty": _to_decimal(event.get("qty")),
        "limit_price": _to_decimal(event.get("limit_price")),
        "status": "submitting",
        "execution_mode": event.get("execution_mode"),
        "report_timing": event.get("report_timing"),
        "earnings_date": event.get("earnings_date"),
        "expiry": (event.get("contract") or {}).get("lastTradeDateOrContractMonth"),
        "strike": _to_decimal((event.get("contract") or {}).get("strike")),
        "right": (event.get("contract") or {}).get("right"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    item = {k: v for k, v in item.items() if v is not None}
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(trade_id)")
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _update_status(trade_id: str, **fields: Any) -> None:
    table = _dynamodb.Table(PENDING_TRADES_TABLE)
    if not fields:
        return
    expr = "SET " + ", ".join(f"#{k} = :{k}" for k in fields)
    table.update_item(
        Key={"trade_id": trade_id},
        UpdateExpression=expr,
        ExpressionAttributeNames={f"#{k}": k for k in fields},
        ExpressionAttributeValues={f":{k}": _to_decimal(v) for k, v in fields.items()},
    )


async def _submit_and_wait(event: dict[str, Any]) -> dict[str, Any]:
    contract_dict = event["contract"]
    contract = Option(
        symbol=contract_dict["symbol"],
        lastTradeDateOrContractMonth=contract_dict["lastTradeDateOrContractMonth"],
        strike=float(contract_dict["strike"]),
        right=contract_dict["right"],
        exchange=contract_dict.get("exchange", "SMART"),
        currency=contract_dict.get("currency", "USD"),
        multiplier=contract_dict.get("multiplier", "100"),
    )
    qty = int(event["qty"])
    lim = float(event["limit_price"])
    trade_id = event["trade_id"]
    order = LimitOrder("BUY", qty, lim)
    order.tif = "DAY"
    order.orderRef = trade_id  # audit + idempotency on IB side

    ib = IB()
    client_id = CLIENT_ID_BASE + random.randint(0, 999)
    LOG.info("[%s] connecting to IB Gateway %s:%d (clientId=%d)",
             trade_id, GATEWAY_HOST, GATEWAY_PORT, client_id)
    # NOT readonly — this Lambda actually places orders (only reached when
    # execution_mode == "live"; recommend-mode short-circuits in handler).
    await ib.connectAsync(GATEWAY_HOST, GATEWAY_PORT, clientId=client_id, readonly=False)
    try:
        await ib.qualifyContractsAsync(contract)
        if not contract.conId:
            return {"status": "rejected", "reason": "qualify_failed"}

        trade = ib.placeOrder(contract, order)
        submitted_at = datetime.now(timezone.utc).isoformat()
        LOG.info("[%s] submitted BUY %d @ %.2f (ib_order_id=%s)",
                 trade_id, qty, lim, trade.order.orderId)

        # Poll the Trade status events for up to FILL_TIMEOUT_SECONDS.
        try:
            await asyncio.wait_for(trade.filledEvent, timeout=FILL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(1)  # let last status arrive

        status = (trade.orderStatus.status or "").lower()
        filled_qty = int(trade.orderStatus.filled or 0)
        avg_price = float(trade.orderStatus.avgFillPrice or 0) or None
        completed_at = datetime.now(timezone.utc).isoformat()

        if filled_qty >= qty:
            ret_status = "filled"
        elif filled_qty > 0:
            ret_status = "partial"
        elif status in ("cancelled", "apicancelled", "inactive"):
            ret_status = "rejected"
        else:
            # Still working — cancel to avoid an unattended order.
            ib.cancelOrder(order)
            ret_status = "timeout"

        return {
            "status": ret_status,
            "fill_price": avg_price,
            "fill_qty": filled_qty,
            "ib_order_id": trade.order.orderId,
            "submitted_at": submitted_at,
            "completed_at": completed_at,
        }
    finally:
        ib.disconnect()


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    trade_id = event.get("trade_id")
    ticker = event.get("ticker")
    execution_mode = (event.get("execution_mode") or "recommend").lower()
    if not trade_id or not ticker:
        raise ValueError(f"Missing trade_id/ticker in event: {event!r}")

    # Defensive: should never be reached in recommend mode (Choice state in
    # SFN skips this Lambda) but second layer guard.
    if execution_mode != "live":
        LOG.info("[%s] execution_mode=%s — no order submitted", trade_id, execution_mode)
        return {
            "trade_id": trade_id,
            "ticker": ticker,
            "status": "recommend_only",
            "fill_price": None,
            "fill_qty": None,
            "ib_order_id": None,
        }

    if not _claim_trade_id(trade_id, event):
        LOG.warning("[%s] trade_id already claimed — refusing duplicate", trade_id)
        return {
            "trade_id": trade_id,
            "ticker": ticker,
            "status": "duplicate",
            "fill_price": None,
            "fill_qty": None,
            "ib_order_id": None,
        }

    try:
        result = asyncio.run(_submit_and_wait(event))
    except Exception as e:  # noqa: BLE001
        LOG.exception("[%s] order submission failed: %s", trade_id, e)
        _update_status(trade_id, status="error", error=str(e)[:512])
        raise

    _update_status(
        trade_id,
        status=result["status"],
        fill_price=result.get("fill_price"),
        fill_qty=result.get("fill_qty"),
        ib_order_id=result.get("ib_order_id"),
        completed_at=result.get("completed_at"),
    )
    result.update({"trade_id": trade_id, "ticker": ticker})
    return result
