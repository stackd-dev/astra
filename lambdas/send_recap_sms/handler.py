"""SendRecapSMS Lambda — publishes the day's entry recap to the alerts SNS
topic (which fans out to the owner's SMS endpoint).

One message, kept short enough to land cleanly in a 1-2 segment SMS. Carries
the run mode (RECOMMEND or LIVE), each pick with fill status, the morning
exit reminder, and (when applicable) the STOP-file abort flag.

Input event: the full state from the AfternoonEntry SFN — typically merged
output of PreFlightChecks + Map(OrderExecutor) + RecordTrades.

Required environment variables:
    ALERTS_TOPIC_ARN     CoreStack alertsTopic ARN
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import boto3

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

ALERTS_TOPIC_ARN = os.environ["ALERTS_TOPIC_ARN"]
ET = ZoneInfo("America/New_York")

_sns = boto3.client("sns")


def _format_pick(pick: dict[str, Any]) -> str:
    side = (pick.get("side") or "").replace("long_", "").upper()
    ticker = pick.get("ticker", "?")
    contract = pick.get("contract") or {}
    strike = contract.get("strike")
    right = contract.get("right")
    expiry = contract.get("lastTradeDateOrContractMonth", "?")
    qty = pick.get("qty")
    fill_price = pick.get("fill_price")
    limit_price = pick.get("limit_price")
    status = pick.get("status") or "?"
    price = fill_price if fill_price is not None else limit_price
    return f"{ticker} {side} {qty}x {strike}{right} {expiry} @ ${price} [{status}]"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    target_T = event.get("target_T", "?")
    mode = (event.get("execution_mode") or "recommend").upper()
    stop = bool(event.get("stop_file_active"))
    past_cutoff = bool(event.get("past_cutoff"))
    approved = event.get("approved_picks") or []
    rejected = event.get("rejected") or []
    order_results = {r.get("trade_id"): r for r in (event.get("order_results") or []) if r.get("trade_id")}

    now_et = datetime.now(ET).strftime("%Y-%m-%d %H:%M %Z")

    lines: list[str] = []
    lines.append(f"ASTRA {mode} {target_T} ({now_et})")
    if stop:
        lines.append("ABORTED: STOP file active")
    if past_cutoff:
        lines.append("ABORTED: past 3:55pm ET entry cutoff")

    enriched: list[dict[str, Any]] = []
    for pick in approved:
        merged = dict(pick)
        order = order_results.get(pick.get("trade_id"))
        if order:
            merged.update({
                "fill_price": order.get("fill_price"),
                "fill_qty": order.get("fill_qty"),
                "status": order.get("status"),
                "ib_order_id": order.get("ib_order_id"),
            })
        elif mode == "RECOMMEND":
            merged["status"] = "recommended"
        enriched.append(merged)

    if not enriched and not stop and not past_cutoff:
        lines.append("No picks today.")
    else:
        for pick in enriched:
            lines.append("- " + _format_pick(pick))

    if rejected:
        lines.append(f"Rejected: {len(rejected)}")
        for r in rejected[:3]:
            lines.append(f"  {r.get('ticker', '?')}: {r.get('reason', '?')}")
        if len(rejected) > 3:
            lines.append(f"  (+{len(rejected) - 3} more)")

    if enriched:
        lines.append("EXIT: manual at next market open.")

    body = "\n".join(lines)
    LOG.info("SMS recap (%d chars):\n%s", len(body), body)

    resp = _sns.publish(
        TopicArn=ALERTS_TOPIC_ARN,
        Subject=f"Astra {mode} recap {target_T}",
        Message=body,
    )
    return {"message_id": resp.get("MessageId"), "length": len(body)}
