"""SentimentProcessor Lambda — scores recent news/social content per candidate
ticker via Amazon Bedrock claude-haiku-4-5 and writes the result to
sentiment_scores.

Runs in the AfternoonEntry SFN right before StrategyEngine so the scores
reflect the most recent intraday mentions, not yesterday's.

Input event (whole-batch invocation, not Map):
    {
      "target_T": "2026-05-26",
      "candidates": [
        {"ticker": "NVDA", "earnings_date": "2026-05-27", ...},
        ...
      ],
      "window_hours": 24       # optional, defaults to 24
    }

Return value:
    {
      "target_T": "2026-05-26",
      "scored": <int>,
      "skipped": <int>,
      "ticker_scores": {
         "NVDA": {sentiment_score, conviction, volume_signal,
                  is_contrarian_setup, key_themes, noise_ratio},
         ...
      }
    }

Per CLAUDE.md §8 the output schema is JSON-only and fixed:
  sentiment_score        float in [-1, 1]
  conviction             float in [0, 1]
  volume_signal          one of "low", "normal", "high"
  is_contrarian_setup    bool
  key_themes             list[str]
  noise_ratio            float in [0, 1]

The model gets all mentions for one ticker stitched into one prompt. We use
Bedrock's `invoke_model` (not the converse API) so we can pin a strict
response schema via the system prompt. VADER fallback (CLAUDE.md §8) kicks
in if the Bedrock call errors so the scorer always has some signal.

Required environment variables:
    SEEN_CONTENT_TABLE_NAME       CoreStack seen_content table
    SENTIMENT_SCORES_TABLE_NAME   CoreStack sentiment_scores table
    BEDROCK_MODEL_ID              e.g. anthropic.claude-haiku-4-5-20251001-v1:0
    BEDROCK_REGION                e.g. us-east-1
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

SEEN_TABLE = os.environ["SEEN_CONTENT_TABLE_NAME"]
SCORES_TABLE = os.environ["SENTIMENT_SCORES_TABLE_NAME"]
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-haiku-4-5-20251001-v1:0")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "us-east-1")

_dynamodb = boto3.resource("dynamodb")
_bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)

SYSTEM_PROMPT = """You are a sentiment analyst for an options trading system that
trades the OVERNIGHT earnings move. You read raw social and news mentions for
ONE ticker and emit a single JSON object on the schema below. No preamble, no
markdown, no explanation outside the JSON. If the input is sparse or ambiguous,
return conservative scores (sentiment_score near 0, conviction low).

Schema (all fields required):
{
  "sentiment_score": float in [-1, 1],   # -1 = strongly bearish, +1 = strongly bullish
  "conviction": float in [0, 1],         # how much signal vs noise
  "volume_signal": "low" | "normal" | "high",
  "is_contrarian_setup": boolean,        # crowd extremely one-sided
  "key_themes": [string, ...],           # up to 5 short phrases
  "noise_ratio": float in [0, 1]         # 0 = pure signal, 1 = pure noise/jokes
}

Treat WSB-style sarcasm, memes, and "to the moon"/"puts printing" jokes as
sentiment carriers, not as noise — they're directional. Only true off-topic
posts count as noise. Output ONLY the JSON object."""


def _to_decimal(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
        return Decimal(str(v))
    return v


def _collect_mentions(ticker: str, since: datetime) -> list[dict[str, Any]]:
    """Scan seen_content for items mentioning `ticker` since `since`.

    seen_content is the dedup + raw-content table (CoreStack). Items written
    by the ingestion Fargate task carry: content_hash (pk), ticker, source,
    content, ingested_at, ttl. Scan is fine for the volumes we expect
    (low-thousands of rows in the 24h window per ticker)."""
    table = _dynamodb.Table(SEEN_TABLE)
    since_iso = since.isoformat()
    kwargs: dict[str, Any] = {
        "FilterExpression": Attr("ticker").eq(ticker) & Attr("ingested_at").gte(since_iso),
    }
    mentions: list[dict[str, Any]] = []
    while True:
        resp = table.scan(**kwargs)
        mentions.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return mentions


def _build_user_prompt(ticker: str, mentions: list[dict[str, Any]]) -> str:
    if not mentions:
        return (
            f"Ticker: {ticker}\n"
            f"No mentions in the window.\n"
            "Return conservative defaults (sentiment_score 0, conviction 0)."
        )
    lines = [f"Ticker: {ticker}", f"Mentions ({len(mentions)} items):"]
    for m in mentions[:200]:  # cap context window
        source = m.get("source", "unknown")
        content = (m.get("content") or "")[:500]
        ingested = m.get("ingested_at", "")
        lines.append(f"- [{source} {ingested}] {content}")
    return "\n".join(lines)


def _invoke_bedrock(user_prompt: str) -> dict[str, Any]:
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
        "temperature": 0.0,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    resp = _bedrock.invoke_model(
        modelId=MODEL_ID, body=json.dumps(body), contentType="application/json",
    )
    raw = json.loads(resp["body"].read())
    text = "".join(b.get("text", "") for b in raw.get("content", []) if b.get("type") == "text").strip()
    # Strip any code fence the model might add despite instructions.
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    return json.loads(text)


def _vader_fallback(ticker: str, mentions: list[dict[str, Any]]) -> dict[str, Any]:
    """Cheap last-resort scoring if Bedrock errors. Matches the schema."""
    if not mentions:
        return {
            "sentiment_score": 0.0, "conviction": 0.0, "volume_signal": "low",
            "is_contrarian_setup": False, "key_themes": [], "noise_ratio": 1.0,
        }
    positive = ("buy", "calls", "long", "bull", "moon", "rip", "beat", "crush", "rocket")
    negative = ("sell", "puts", "short", "bear", "miss", "tank", "dump", "crash")
    pos_count = neg_count = 0
    for m in mentions:
        txt = (m.get("content") or "").lower()
        pos_count += sum(txt.count(w) for w in positive)
        neg_count += sum(txt.count(w) for w in negative)
    total = pos_count + neg_count
    score = 0.0 if total == 0 else (pos_count - neg_count) / total
    vol = "low" if len(mentions) < 20 else "high" if len(mentions) > 200 else "normal"
    return {
        "sentiment_score": score,
        "conviction": min(1.0, total / 50.0),
        "volume_signal": vol,
        "is_contrarian_setup": False,
        "key_themes": [],
        "noise_ratio": 0.5,
    }


def _score_ticker(ticker: str, since: datetime) -> tuple[dict[str, Any], int]:
    mentions = _collect_mentions(ticker, since)
    LOG.info("[%s] %d mentions in window", ticker, len(mentions))
    user_prompt = _build_user_prompt(ticker, mentions)
    try:
        scored = _invoke_bedrock(user_prompt)
    except Exception as e:  # noqa: BLE001
        LOG.warning("[%s] Bedrock invoke failed (%s); using VADER fallback", ticker, e)
        scored = _vader_fallback(ticker, mentions)
    return scored, len(mentions)


def _write_score(ticker: str, target_T: str, scored: dict[str, Any], mention_count: int) -> None:
    table = _dynamodb.Table(SCORES_TABLE)
    item = {
        "ticker": ticker,
        "date": target_T,
        "sentiment_score": _to_decimal(scored.get("sentiment_score")),
        "conviction": _to_decimal(scored.get("conviction")),
        "volume_signal": scored.get("volume_signal"),
        "is_contrarian_setup": bool(scored.get("is_contrarian_setup", False)),
        "key_themes": scored.get("key_themes") or [],
        "noise_ratio": _to_decimal(scored.get("noise_ratio")),
        "mention_count": mention_count,
        "scored_at": datetime.now(timezone.utc).isoformat(),
        "model_id": MODEL_ID,
    }
    item = {k: v for k, v in item.items() if v is not None}
    table.put_item(Item=item)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    target_T = event.get("target_T") or datetime.now(timezone.utc).date().isoformat()
    candidates = event.get("candidates") or []
    window_hours = int(event.get("window_hours", 24))
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    ticker_scores: dict[str, dict[str, Any]] = {}
    scored_count = 0
    skipped_count = 0
    for cand in candidates:
        ticker = cand.get("ticker")
        if not ticker:
            skipped_count += 1
            continue
        try:
            scored, mention_count = _score_ticker(ticker, since)
            _write_score(ticker, target_T, scored, mention_count)
            ticker_scores[ticker] = scored
            scored_count += 1
        except Exception:  # noqa: BLE001
            LOG.exception("[%s] sentiment scoring failed", ticker)
            skipped_count += 1
    LOG.info("SentimentProcessor: scored=%d skipped=%d", scored_count, skipped_count)
    return {
        "target_T": target_T,
        "scored": scored_count,
        "skipped": skipped_count,
        "ticker_scores": ticker_scores,
    }
