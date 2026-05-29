"""EarningsFetcher Lambda — nightly job that pulls upcoming earnings events
from Finnhub's calendar API and writes the *actionable* AMC/BMO candidates
to the earnings_calendar DynamoDB table.

The "actionable" filter is anchored to T, the date of the next entry-window
afternoon (CLAUDE.md §2, docs/earnings-system-design.md §3.1):
  - AMC on T:   report tonight after-close → enter T afternoon
  - BMO on T+1: report next morning pre-open → enter T afternoon
Anything else (past events, AMC on T+1, BMO on T) is unactionable for this
cycle and is dropped.

T is recomputed at invocation time:
  - now_PT < 13:00 PT  ⇒  T = today (entry window is still later today)
  - otherwise          ⇒  T = next non-weekend day

Required environment variables:
    EARNINGS_CALENDAR_TABLE_NAME  CoreStack's earnings_calendar table name
    DATA_FEED_SECRET_ARN          ARN of astra/data-feed-credentials secret
                                  (JSON must contain "finnhub_api_key")
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import boto3

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

TABLE_NAME = os.environ["EARNINGS_CALENDAR_TABLE_NAME"]
SECRET_ARN = os.environ["DATA_FEED_SECRET_ARN"]
FINNHUB_BASE = "https://finnhub.io/api/v1"

PT = ZoneInfo("America/Los_Angeles")
# US equities close at 1pm PT (4pm ET). After that, today's entry window is
# gone and T rolls to the next trading day.
MARKET_CLOSE_PT_HOUR = 13

_dynamodb = boto3.resource("dynamodb")
_secretsmanager = boto3.client("secretsmanager")

_finnhub_api_key: str | None = None


def _api_key() -> str:
    """Read finnhub_api_key from Secrets Manager. Cached across warm invocations."""
    global _finnhub_api_key
    if _finnhub_api_key is None:
        secret = _secretsmanager.get_secret_value(SecretId=SECRET_ARN)
        payload = json.loads(secret["SecretString"])
        key = payload.get("finnhub_api_key")
        if not key:
            raise RuntimeError(
                "finnhub_api_key not set in DataFeedCredentialsSecret"
            )
        _finnhub_api_key = key
    return _finnhub_api_key


def _next_trading_day(d: date) -> date:
    """Return the next non-weekend day after d. Holiday-blind in v1."""
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:  # 5=Sat, 6=Sun
        nxt += timedelta(days=1)
    return nxt


def _next_entry_day(now_utc: datetime) -> date:
    """Compute T — the date of the next entry-window afternoon.

    Holidays are not handled in v1; only weekends are skipped. If T lands on
    a holiday, that night's run just finds no candidates — fail safe.
    """
    now_pt = now_utc.astimezone(PT)
    today_close_pt = now_pt.replace(
        hour=MARKET_CLOSE_PT_HOUR, minute=0, second=0, microsecond=0
    )
    T = now_pt.date() if now_pt < today_close_pt else now_pt.date() + timedelta(days=1)
    while T.weekday() >= 5:
        T += timedelta(days=1)
    return T


def _fetch_calendar(start: date, end: date) -> list[dict[str, Any]]:
    qs = urlencode(
        {"from": start.isoformat(), "to": end.isoformat(), "token": _api_key()}
    )
    url = f"{FINNHUB_BASE}/calendar/earnings?{qs}"
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=20) as resp:
        body = json.loads(resp.read().decode())
    events = body.get("earningsCalendar") or []
    LOG.info("Finnhub returned %d events for %s..%s", len(events), start, end)
    return events


def _fetch_past_earnings_dates(ticker: str, limit: int = 8) -> list[str]:
    """Past earnings dates for one ticker from Finnhub /stock/earnings.

    This is called from EarningsFetcher (NOT HistoricalMovesFetcher) because
    HistoricalMovesFetcher runs inside the VPC for IB Gateway access — and
    VPC Lambdas have no internet egress (no NAT Gateway by design). We pull
    the dates here, where we already have public-internet networking and a
    cached Finnhub key, and pass them through the candidate payload."""
    qs = urlencode({"symbol": ticker, "token": _api_key()})
    url = f"{FINNHUB_BASE}/stock/earnings?{qs}"
    req = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
    except Exception as e:  # noqa: BLE001
        LOG.warning("[%s] Finnhub /stock/earnings failed: %s", ticker, e)
        return []
    if not isinstance(body, list):
        LOG.warning("[%s] unexpected /stock/earnings response: %r", ticker, body)
        return []
    out: list[str] = []
    for entry in body[:limit]:
        period = entry.get("period")
        if not period:
            continue
        # Validate it's a date so HistoricalMovesFetcher can trust the format.
        try:
            date.fromisoformat(period)
            out.append(period)
        except ValueError:
            continue
    return out


def _to_decimal(v: Any) -> Any:
    """DynamoDB rejects floats — convert any numeric value to Decimal via str."""
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    return v


def _write_actionable(
    events: list[dict[str, Any]], T: date, Tp1: date
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Write actionable events to DDB and return them as candidate dicts for
    the downstream SFN Map state."""
    T_str, Tp1_str = T.isoformat(), Tp1.isoformat()
    table = _dynamodb.Table(TABLE_NAME)
    candidates: list[dict[str, Any]] = []
    skipped_no_timing = 0
    skipped_unactionable = 0
    with table.batch_writer() as batch:
        for ev in events:
            hour = (ev.get("hour") or "").lower()
            if hour not in ("amc", "bmo"):
                skipped_no_timing += 1
                continue
            d = ev.get("date")
            symbol = ev.get("symbol")
            if not (d and symbol):
                continue
            actionable = (d == T_str and hour == "amc") or (
                d == Tp1_str and hour == "bmo"
            )
            if not actionable:
                skipped_unactionable += 1
                continue
            item: dict[str, Any] = {
                "date": d,
                "ticker": symbol,
                "report_timing": hour,
                "target_T": T_str,
                "eps_estimate": _to_decimal(ev.get("epsEstimate")),
                "eps_actual": _to_decimal(ev.get("epsActual")),
                "revenue_estimate": _to_decimal(ev.get("revenueEstimate")),
                "revenue_actual": _to_decimal(ev.get("revenueActual")),
                "quarter": ev.get("quarter"),
                "year": ev.get("year"),
            }
            item = {k: v for k, v in item.items() if v is not None}
            batch.put_item(Item=item)
            # Slim, JSON-safe candidate dict for SFN Map: only the keys that
            # downstream Lambdas actually need as their per-invocation input.
            # past_earnings_dates is included so HistoricalMovesFetcher (which
            # has no internet egress from inside the VPC) can hit IB without
            # also needing Finnhub.
            past_dates = _fetch_past_earnings_dates(symbol, limit=8)
            candidates.append({
                "ticker": symbol,
                "earnings_date": d,
                "report_timing": hour,
                "target_T": T_str,
                "past_earnings_dates": past_dates,
            })
    LOG.info(
        "Wrote %d actionable rows; skipped %d unactionable, %d with no AMC/BMO flag.",
        len(candidates),
        skipped_unactionable,
        skipped_no_timing,
    )
    return candidates, {
        "skipped_unactionable": skipped_unactionable,
        "skipped_no_timing": skipped_no_timing,
    }


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry. Picks T, writes the actionable rows to earnings_calendar,
    and returns the candidate list as `candidates` so the SFN Map state can
    fan out one HistoricalMovesFetcher / OptionsChainFetcher per ticker."""
    now_utc = datetime.now(timezone.utc)
    T = _next_entry_day(now_utc)
    Tp1 = _next_trading_day(T)
    LOG.info("now_utc=%s  T=%s  Tp1=%s", now_utc.isoformat(), T, Tp1)
    events = _fetch_calendar(T, Tp1)
    candidates, stats = _write_actionable(events, T, Tp1)
    return {
        "target_T": T.isoformat(),
        "target_Tp1": Tp1.isoformat(),
        "fetched": len(events),
        "written": len(candidates),
        "candidates": candidates,
        **stats,
    }
