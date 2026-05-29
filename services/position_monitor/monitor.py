"""PositionMonitor — always-on Fargate task.

Three responsibilities per CLAUDE.md §10 + §2:
  1. AMC after-hours preview (T evening, 4:15pm-8pm ET).
     For each open AMC position, periodically polls the underlying's
     after-hours price via IB Gateway and logs the preview move. Does
     NOT trade (no retail options after-hours).
  2. Morning exit alert (T+1 9:30am ET, also any BMO open).
     Sends SMS with each open position + current option price so the
     owner can manually exit at the bell.
  3. Dead-man's switch.
     If the loop hasn't completed a heartbeat in HEARTBEAT_MAX_GAP_SECONDS,
     publishes a critical alert SNS message — an unmonitored overnight
     position is the worst case.

Required environment variables:
    PENDING_TRADES_TABLE_NAME    DDB — open positions to watch
    LIVE_POSITIONS_TABLE_NAME    DDB — heartbeats / current-price snapshots
    ALERTS_TOPIC_ARN             SNS topic for SMS
    GATEWAY_HOST                 Cloud Map name (ib-gateway.astra.local)
    GATEWAY_PORT                 4004 paper / 4003 live
    GATEWAY_CLIENT_ID            ib_async client id; default 31
    POLL_INTERVAL_SECONDS        default 60
    HEARTBEAT_MAX_GAP_SECONDS    default 600
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, time as dtime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import boto3
from boto3.dynamodb.conditions import Attr
from ib_async import IB, Option, Stock

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("monitor")

PENDING_TABLE = os.environ["PENDING_TRADES_TABLE_NAME"]
LIVE_TABLE = os.environ["LIVE_POSITIONS_TABLE_NAME"]
ALERTS_TOPIC_ARN = os.environ["ALERTS_TOPIC_ARN"]
GATEWAY_HOST = os.environ["GATEWAY_HOST"]
GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "4004"))
CLIENT_ID = int(os.environ.get("GATEWAY_CLIENT_ID", "31"))
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
HEARTBEAT_MAX_GAP_SECONDS = int(os.environ.get("HEARTBEAT_MAX_GAP_SECONDS", "600"))

ET = ZoneInfo("America/New_York")
_dynamodb = boto3.resource("dynamodb")
_sns = boto3.client("sns")

_last_heartbeat = time.time()
# When we already sent today's exit alert per ticker (avoid spam).
_exit_alerted_today: dict[str, str] = {}


def _to_decimal(v: Any) -> Any:
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    return v


def _open_positions() -> list[dict[str, Any]]:
    table = _dynamodb.Table(PENDING_TABLE)
    resp = table.scan(FilterExpression=Attr("status").eq("open"))
    return resp.get("Items", [])


async def _price_option(ib: IB, pos: dict[str, Any]) -> tuple[float | None, float | None]:
    """Return (option_mid, underlying_last). Either may be None on a sparse market."""
    contract = Option(
        symbol=pos["ticker"],
        lastTradeDateOrContractMonth=pos["expiry"],
        strike=float(pos["strike"]),
        right=pos["right"],
        exchange="SMART",
        currency="USD",
        multiplier="100",
    )
    underlying = Stock(pos["ticker"], "SMART", "USD")
    try:
        await ib.qualifyContractsAsync(contract, underlying)
    except Exception as e:  # noqa: BLE001
        LOG.warning("[%s] qualify failed: %s", pos.get("trade_id"), e)
        return None, None

    opt_t = ib.reqMktData(contract, "", snapshot=True, regulatorySnapshot=False)
    und_t = ib.reqMktData(underlying, "", snapshot=True, regulatorySnapshot=False)
    await asyncio.sleep(3)
    ib.cancelMktData(contract)
    ib.cancelMktData(underlying)

    bid, ask = getattr(opt_t, "bid", None), getattr(opt_t, "ask", None)
    last = getattr(opt_t, "last", None)
    close = getattr(opt_t, "close", None)
    mid: float | None = None
    if bid and ask and bid > 0 and ask > 0:
        mid = (float(bid) + float(ask)) / 2
    elif last and last > 0:
        mid = float(last)
    elif close and close > 0:
        mid = float(close)

    und_last = getattr(und_t, "last", None) or getattr(und_t, "close", None)
    und_last = float(und_last) if und_last and und_last > 0 else None
    return mid, und_last


def _write_snapshot(pos: dict[str, Any], opt_mid: float | None, und_last: float | None, phase: str) -> None:
    table = _dynamodb.Table(LIVE_TABLE)
    table.put_item(
        Item={
            "position_id": f"{pos['trade_id']}-{phase}-{int(time.time())}",
            "trade_id": pos["trade_id"],
            "ticker": pos["ticker"],
            "phase": phase,
            "option_mid": _to_decimal(opt_mid),
            "underlying_last": _to_decimal(und_last),
            "entry_price": _to_decimal(pos.get("entry_price")),
            "snapshot_at": datetime.now(timezone.utc).isoformat(),
        }
    )


def _send_sms(subject: str, body: str) -> None:
    LOG.info("Publishing alert: %s\n%s", subject, body)
    _sns.publish(TopicArn=ALERTS_TOPIC_ARN, Subject=subject[:99], Message=body)


def _phase_for_now(now_et: datetime) -> str:
    """Wall-clock phase used to bucket snapshots + alert decisions."""
    t = now_et.time()
    if dtime(9, 25) <= t <= dtime(9, 45):
        return "exit_window"          # Morning bell — alert + price
    if dtime(16, 15) <= t <= dtime(20, 0):
        return "afterhours_preview"   # AMC preview window
    if dtime(4, 0) <= t <= dtime(9, 20):
        return "premarket"            # BMO context
    return "idle"


async def _process_phase(ib: IB, positions: list[dict[str, Any]], phase: str, now_et: datetime) -> None:
    today_iso = now_et.date().isoformat()
    exit_lines: list[str] = []
    for pos in positions:
        opt_mid, und_last = await _price_option(ib, pos)
        _write_snapshot(pos, opt_mid, und_last, phase)
        entry = float(pos.get("entry_price") or 0)
        delta_pct = None
        if entry > 0 and opt_mid is not None:
            delta_pct = (opt_mid - entry) / entry * 100.0
        LOG.info(
            "[%s] %s pos=%s entry=%.2f mid=%s und=%s delta=%s",
            pos.get("ticker"), phase, pos.get("trade_id"), entry,
            f"{opt_mid:.2f}" if opt_mid else "n/a",
            f"{und_last:.2f}" if und_last else "n/a",
            f"{delta_pct:+.1f}%" if delta_pct is not None else "n/a",
        )
        if phase == "exit_window":
            side = pos.get("side", "?")
            qty = pos.get("qty", "?")
            strike = pos.get("strike", "?")
            right = pos.get("right", "?")
            expiry = pos.get("expiry", "?")
            line = (
                f"{pos['ticker']} {side} {qty}x {strike}{right} {expiry} "
                f"entry={entry:.2f} now={opt_mid:.2f if opt_mid else 'n/a'} "
                f"({delta_pct:+.1f}%)" if delta_pct is not None else ""
            )
            exit_lines.append(line)

    if phase == "exit_window" and exit_lines:
        already = _exit_alerted_today.get("date") == today_iso
        if not already:
            body = "EXIT NOW (manual):\n" + "\n".join(exit_lines)
            _send_sms(f"Astra EXIT {today_iso}", body)
            _exit_alerted_today.clear()
            _exit_alerted_today["date"] = today_iso


async def _monitor_loop() -> None:
    global _last_heartbeat
    while True:
        try:
            positions = _open_positions()
            now_et = datetime.now(ET)
            phase = _phase_for_now(now_et)
            if positions and phase != "idle":
                ib = IB()
                await ib.connectAsync(GATEWAY_HOST, GATEWAY_PORT, clientId=CLIENT_ID, readonly=True)
                ib.reqMarketDataType(4)
                try:
                    await _process_phase(ib, positions, phase, now_et)
                finally:
                    ib.disconnect()
            else:
                LOG.info("Heartbeat: %d open positions, phase=%s, now_et=%s",
                         len(positions), phase, now_et.isoformat())
            _last_heartbeat = time.time()
        except Exception:  # noqa: BLE001
            LOG.exception("monitor loop iteration failed")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _watchdog_loop() -> None:
    """Independent watchdog — if the monitor loop hasn't beaten in
    HEARTBEAT_MAX_GAP_SECONDS, alert."""
    alerted = False
    while True:
        gap = time.time() - _last_heartbeat
        if gap > HEARTBEAT_MAX_GAP_SECONDS:
            if not alerted:
                _send_sms(
                    "Astra MONITOR STALE",
                    f"PositionMonitor heartbeat gap = {int(gap)}s. Investigate.",
                )
                alerted = True
        else:
            alerted = False
        await asyncio.sleep(60)


async def _main_async() -> None:
    LOG.info("PositionMonitor starting")
    await asyncio.gather(_monitor_loop(), _watchdog_loop())


if __name__ == "__main__":
    asyncio.run(_main_async())
