"""StrategyEngine Lambda — turns research outputs into a ranked list of picks.

For each candidate it:
  1. Reads the chain snapshot meta JSON from S3 (today's fresh chain
     aggregates written by OptionsChainFetcher).
  2. Reads the historical_moves rows from DDB (past N quarters' actual
     moves, written by HistoricalMovesFetcher).
  3. Reads today's sentiment_scores row from DDB.
  4. Computes edge_ratio = mean(historical abs moves) / implied_move_pct.
  5. Computes liquidity + spread penalty from the chain meta.
  6. Picks a contract: slightly OTM weekly post-earnings expiry, target
     delta 0.30-0.40. Calls vs puts vs straddle based on a direction
     confidence threshold (CLAUDE.md §7 — directional signals are weak).
  7. Sizes per CLAUDE.md §7: max_loss_per_trade = SIZING_PCT of capital.
  8. Returns the picks list for the next SFN step (PreFlightChecks).

Scoring formula (weights from S3 config; never hardcoded):
    score = w1*edge_ratio
          + w2*direction_confidence
          + w3*liquidity
          - w4*spread_penalty
          + w5*sentiment

Input event:
    {
      "target_T": "2026-05-26",
      "candidates": [{ticker, earnings_date, report_timing, target_T}, ...],
      "chain_results": [{ticker, status, s3_meta_key, ...}, ...],
      "sentiment_result": {ticker_scores: {...}, ...}
    }

Return:
    {
      "target_T": "2026-05-26",
      "picks": [{trade_id, ticker, contract, side, qty, limit_price,
                 score, score_components, reasoning, ...}, ...],
      "considered": int,
      "filtered": int
    }

Required environment variables:
    HISTORICAL_MOVES_TABLE_NAME    DDB
    SENTIMENT_SCORES_TABLE_NAME    DDB
    CONFIG_BUCKET_NAME             S3 (reads chain metas + strategy.yaml weights)
    STRATEGY_CONFIG_KEY            default "strategy.json"
    DEFAULT_CAPITAL_USD            default "5000"
"""

from __future__ import annotations

import json
import logging
import math
import os
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from statistics import mean
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

HIST_TABLE = os.environ["HISTORICAL_MOVES_TABLE_NAME"]
SENTIMENT_TABLE = os.environ["SENTIMENT_SCORES_TABLE_NAME"]
CONFIG_BUCKET = os.environ["CONFIG_BUCKET_NAME"]
STRATEGY_CONFIG_KEY = os.environ.get("STRATEGY_CONFIG_KEY", "strategy.json")
DEFAULT_CAPITAL_USD = float(os.environ.get("DEFAULT_CAPITAL_USD", "5000"))

_dynamodb = boto3.resource("dynamodb")
_s3 = boto3.client("s3")

# Defaults applied when strategy.json is missing — match CLAUDE.md §7
# starting weights ("low w5 sentiment, raise only if review shows signal").
# These are deliberate guesses; ALL of them are tunable from S3.
DEFAULT_CONFIG = {
    "weights": {
        "edge_ratio": 1.0,
        "direction_confidence": 0.6,
        "liquidity": 0.3,
        "spread_penalty": 0.8,
        "sentiment": 0.2,
    },
    "thresholds": {
        "min_score": 0.4,
        "direction_confidence_threshold": 0.55,   # below this, straddle
        "min_edge_ratio": 1.1,                    # below this, skip
    },
    "contract_selection": {
        "target_delta_min": 0.30,
        "target_delta_max": 0.40,
    },
    "sizing": {
        "max_loss_pct_per_trade": 0.30,  # 25-33% of per-trade capital per CLAUDE.md §7
        "max_contracts_per_trade": 5,
    },
}


def _f(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        v = float(v)
    if isinstance(v, (int, float)):
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
        return float(v)
    return None


def _load_config() -> dict[str, Any]:
    try:
        obj = _s3.get_object(Bucket=CONFIG_BUCKET, Key=STRATEGY_CONFIG_KEY)
        cfg = json.loads(obj["Body"].read())
        # Shallow merge so a partial config doesn't blow up missing fields.
        merged = json.loads(json.dumps(DEFAULT_CONFIG))
        for top_key, top_val in cfg.items():
            if isinstance(top_val, dict) and isinstance(merged.get(top_key), dict):
                merged[top_key].update(top_val)
            else:
                merged[top_key] = top_val
        LOG.info("Loaded strategy config from s3://%s/%s", CONFIG_BUCKET, STRATEGY_CONFIG_KEY)
        return merged
    except _s3.exceptions.NoSuchKey:
        LOG.warning("No strategy config at s3://%s/%s; using defaults", CONFIG_BUCKET, STRATEGY_CONFIG_KEY)
        return DEFAULT_CONFIG
    except Exception:  # noqa: BLE001
        LOG.exception("Failed to load strategy config; using defaults")
        return DEFAULT_CONFIG


def _read_chain_meta(ticker: str, target_T: str, s3_key: str | None = None) -> dict[str, Any] | None:
    key = s3_key or f"options-chains/{target_T}/{ticker}.meta.json"
    try:
        obj = _s3.get_object(Bucket=CONFIG_BUCKET, Key=key)
        return json.loads(obj["Body"].read())
    except _s3.exceptions.NoSuchKey:
        LOG.warning("[%s] no chain meta at s3://%s/%s", ticker, CONFIG_BUCKET, key)
        return None
    except Exception:  # noqa: BLE001
        LOG.exception("[%s] failed to read chain meta", ticker)
        return None


def _read_chain_parquet_rows(ticker: str, target_T: str) -> list[dict[str, Any]]:
    """Read the full chain rows from the Parquet snapshot so we can pick a
    specific contract by delta. Lazily imports pyarrow."""
    import io
    import pyarrow.parquet as pq

    key = f"options-chains/{target_T}/{ticker}.parquet"
    try:
        obj = _s3.get_object(Bucket=CONFIG_BUCKET, Key=key)
        buf = io.BytesIO(obj["Body"].read())
        table = pq.read_table(buf)
        return table.to_pylist()
    except Exception:  # noqa: BLE001
        LOG.exception("[%s] failed to read chain parquet", ticker)
        return []


def _read_historical_moves(ticker: str) -> list[float]:
    table = _dynamodb.Table(HIST_TABLE)
    resp = table.query(KeyConditionExpression=Key("ticker").eq(ticker))
    moves: list[float] = []
    for item in resp.get("Items", []):
        mv = _f(item.get("actual_move_pct"))
        if mv is not None:
            moves.append(mv)
    return moves


def _read_sentiment(ticker: str, target_T: str) -> dict[str, Any] | None:
    table = _dynamodb.Table(SENTIMENT_TABLE)
    resp = table.get_item(Key={"ticker": ticker, "date": target_T})
    return resp.get("Item")


def _direction_confidence(sent: dict[str, Any] | None, chain_meta: dict[str, Any]) -> tuple[float, str]:
    """Return (confidence in [0,1], side in {"long_call","long_put","straddle"}).

    Direction is hard per CLAUDE.md §7 — combine two weak signals:
      - Sentiment (positive → call bias, negative → put bias)
      - Put/call volume + OI ratio relative to 1.0 (>1 = bearish flow)
    Confidence is how far the blended signal is from neutral (0)."""
    sent_score = _f(sent.get("sentiment_score") if sent else None) or 0.0
    pc_vol = _f(chain_meta.get("put_call_volume_ratio")) or 1.0
    pc_oi = _f(chain_meta.get("put_call_oi_ratio")) or 1.0
    flow = -((pc_vol - 1.0) + (pc_oi - 1.0)) / 2.0  # >0 = bullish flow
    flow = max(-1.0, min(1.0, flow))
    blended = 0.6 * sent_score + 0.4 * flow
    side = "long_call" if blended > 0 else "long_put" if blended < 0 else "straddle"
    return abs(blended), side


def _liquidity_score(chain_meta: dict[str, Any]) -> float:
    """Total volume + OI on the expiry, normalized into [0, 1]. Caps high
    so a high-volume name doesn't dominate the score."""
    vol = (_f(chain_meta.get("call_volume_total")) or 0) + (_f(chain_meta.get("put_volume_total")) or 0)
    oi = (_f(chain_meta.get("call_oi_total")) or 0) + (_f(chain_meta.get("put_oi_total")) or 0)
    raw = math.log10(max(vol + oi, 1))  # log scale dampens megacap dominance
    return min(1.0, raw / 6.0)            # ~1M total OI+vol → 1.0


def _spread_penalty(chain_meta: dict[str, Any]) -> float:
    """ATM bid-ask spread / mid, averaged across call+put. 0 = tight book."""
    components = []
    for prefix in ("atm_call", "atm_put"):
        bid = _f(chain_meta.get(f"{prefix}_bid"))
        ask = _f(chain_meta.get(f"{prefix}_ask"))
        mid = _f(chain_meta.get(f"{prefix}_mid"))
        if bid and ask and mid and mid > 0:
            components.append((ask - bid) / mid)
    if not components:
        return 0.5
    return min(1.0, sum(components) / len(components))


def _pick_contract(
    chain_rows: list[dict[str, Any]], side: str, spot: float, cfg: dict[str, Any],
) -> dict[str, Any] | None:
    """Pick a contract in the configured delta band. For straddles caller
    invokes this twice (one call + one put)."""
    if not chain_rows:
        return None
    dlo = cfg["contract_selection"]["target_delta_min"]
    dhi = cfg["contract_selection"]["target_delta_max"]
    right = "C" if side == "long_call" else "P"
    target_sign = 1.0 if right == "C" else -1.0

    candidates = []
    for r in chain_rows:
        if r.get("right") != right:
            continue
        d = _f(r.get("delta"))
        if d is None:
            continue
        abs_d = abs(d)
        if dlo <= abs_d <= dhi and (d * target_sign) > 0:
            candidates.append((abs(abs_d - (dlo + dhi) / 2.0), r))
    if not candidates:
        # Fallback: slightly OTM by moneyness (no Greeks available, e.g.
        # frozen weekend data).
        for r in chain_rows:
            if r.get("right") != right:
                continue
            m = _f(r.get("moneyness"))
            if m is None:
                continue
            otm = (m > 1.0) if right == "C" else (m < 1.0)
            if otm:
                candidates.append((abs(m - (1.05 if right == "C" else 0.95)), r))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def _limit_price(row: dict[str, Any]) -> float | None:
    """Marketable limit at ask (we want fills) — capped to mid + half spread
    if the spread is sane. None if we can't form a price."""
    bid = _f(row.get("bid"))
    ask = _f(row.get("ask"))
    mid = _f(row.get("mid"))
    last = _f(row.get("last"))
    close = _f(row.get("close"))
    if bid and ask and ask > 0:
        return round(ask, 2)
    for fallback in (mid, last, close):
        if fallback and fallback > 0:
            return round(fallback, 2)
    return None


def _size_qty(limit_price: float, cfg: dict[str, Any], capital: float) -> int:
    """Size such that max loss (= premium paid × 100 × qty) ≤ pct of capital."""
    max_loss_pct = cfg["sizing"]["max_loss_pct_per_trade"]
    max_cap = capital * max_loss_pct
    cost_per_contract = limit_price * 100.0
    if cost_per_contract <= 0:
        return 0
    qty = int(max_cap // cost_per_contract)
    return max(0, min(qty, cfg["sizing"]["max_contracts_per_trade"]))


def _build_pick(
    cand: dict[str, Any], chain_meta: dict[str, Any], chain_rows: list[dict[str, Any]],
    sent: dict[str, Any] | None, cfg: dict[str, Any], capital: float,
) -> dict[str, Any] | None:
    ticker = cand["ticker"]
    spot = _f(chain_meta.get("spot"))
    implied = _f(chain_meta.get("implied_move_pct"))
    if not (spot and implied):
        LOG.warning("[%s] missing spot or implied_move", ticker)
        return None

    moves = _read_historical_moves(ticker)
    if not moves:
        LOG.warning("[%s] no historical moves", ticker)
        return None
    edge_ratio = mean(moves) / implied

    direction_conf, side = _direction_confidence(sent, chain_meta)
    liquidity = _liquidity_score(chain_meta)
    spread = _spread_penalty(chain_meta)
    sentiment_score = _f((sent or {}).get("sentiment_score")) or 0.0

    w = cfg["weights"]
    score = (
        w["edge_ratio"] * edge_ratio
        + w["direction_confidence"] * direction_conf
        + w["liquidity"] * liquidity
        - w["spread_penalty"] * spread
        + w["sentiment"] * sentiment_score
    )

    th = cfg["thresholds"]
    if edge_ratio < th["min_edge_ratio"]:
        LOG.info("[%s] skipped: edge_ratio %.3f < %.3f", ticker, edge_ratio, th["min_edge_ratio"])
        return None
    if score < th["min_score"]:
        LOG.info("[%s] skipped: score %.3f < %.3f", ticker, score, th["min_score"])
        return None

    if direction_conf < th["direction_confidence_threshold"]:
        side = "straddle"

    if side == "straddle":
        call = _pick_contract(chain_rows, "long_call", spot, cfg)
        put = _pick_contract(chain_rows, "long_put", spot, cfg)
        legs = [("long_call", call), ("long_put", put)]
    else:
        legs = [(side, _pick_contract(chain_rows, side, spot, cfg))]

    sub_picks: list[dict[str, Any]] = []
    for leg_side, row in legs:
        if row is None:
            LOG.warning("[%s] could not pick contract for %s", ticker, leg_side)
            return None
        lim = _limit_price(row)
        if lim is None:
            LOG.warning("[%s] no limit price for %s strike %s", ticker, leg_side, row.get("strike"))
            return None
        qty = _size_qty(lim, cfg, capital)
        if qty < 1:
            LOG.info("[%s] qty 0 at limit %.2f for %s — skipped", ticker, lim, leg_side)
            return None
        sub_picks.append({
            "trade_id": f"{ticker}-{cand.get('target_T')}-{leg_side}-{uuid.uuid4().hex[:8]}",
            "ticker": ticker,
            "side": leg_side,
            "qty": qty,
            "limit_price": lim,
            "contract": {
                "symbol": ticker,
                "secType": "OPT",
                "exchange": "SMART",
                "currency": "USD",
                "lastTradeDateOrContractMonth": chain_meta.get("expiry"),
                "strike": _f(row.get("strike")),
                "right": "C" if leg_side == "long_call" else "P",
                "multiplier": "100",
            },
            "expiry": chain_meta.get("expiry"),
            "report_timing": cand.get("report_timing"),
            "earnings_date": cand.get("earnings_date"),
            "spot_at_score": spot,
            "implied_move_pct": implied,
            "edge_ratio": edge_ratio,
            "direction_confidence": direction_conf,
            "liquidity": liquidity,
            "spread_penalty": spread,
            "sentiment_score": sentiment_score,
            "score": score,
        })
    return {"ticker": ticker, "legs": sub_picks, "score": score}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    target_T = event.get("target_T") or date.today().isoformat()
    candidates = event.get("candidates") or []
    chain_results = event.get("chain_results") or []
    sentiment_result = event.get("sentiment_result") or {}
    capital = _f(event.get("capital_usd")) or DEFAULT_CAPITAL_USD

    cfg = _load_config()
    chain_meta_by_ticker: dict[str, dict[str, Any]] = {}
    for cr in chain_results:
        if cr and cr.get("status") == "ok":
            meta = _read_chain_meta(cr["ticker"], target_T, cr.get("s3_meta_key"))
            if meta:
                chain_meta_by_ticker[cr["ticker"]] = meta

    sent_by_ticker = sentiment_result.get("ticker_scores") or {}

    picks: list[dict[str, Any]] = []
    filtered = 0
    considered = 0
    for cand in candidates:
        ticker = cand.get("ticker")
        if not ticker:
            continue
        chain_meta = chain_meta_by_ticker.get(ticker)
        if not chain_meta:
            # Fall back to reading from S3 + DDB (chain may have been
            # cached on a prior run).
            chain_meta = _read_chain_meta(ticker, target_T) or {}
        if not chain_meta:
            LOG.info("[%s] no chain — filtered", ticker)
            filtered += 1
            continue
        sent = sent_by_ticker.get(ticker) or _read_sentiment(ticker, target_T)
        chain_rows = _read_chain_parquet_rows(ticker, target_T)
        considered += 1
        result = _build_pick(cand, chain_meta, chain_rows, sent, cfg, capital)
        if result is None:
            filtered += 1
            continue
        picks.extend(result["legs"])
    picks.sort(key=lambda p: p["score"], reverse=True)
    LOG.info("StrategyEngine: considered=%d picks=%d filtered=%d", considered, len(picks), filtered)
    return {
        "target_T": target_T,
        "picks": picks,
        "considered": considered,
        "filtered": filtered,
        "scored_at": datetime.now(timezone.utc).isoformat(),
    }
