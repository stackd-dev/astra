"""NewsRedditIngestion — always-on Fargate task that streams news (Finnhub
WebSocket) and Reddit posts (PRAW) about today's actionable tickers into
the seen_content DynamoDB table.

seen_content's content_hash partition key gives free dedup. The TTL attribute
auto-evicts entries after `INGESTION_TTL_DAYS` (default 3) — enough for
SentimentProcessor to read the prior 24h window even after a Lambda retry.

Three concurrent threads:
  1. _refresh_tickers_loop  — every TICKER_REFRESH_SECONDS, scan
     earnings_calendar for actionable target_T tickers (today + tomorrow).
  2. _news_ws_loop          — Finnhub news WebSocket; subscribes to the
     active ticker set, writes matched items to seen_content.
  3. _reddit_loop           — polls configured subreddits (default:
     wallstreetbets + options + stocks) for posts/comments mentioning
     active tickers; writes matches to seen_content.

Required environment variables:
    EARNINGS_CALENDAR_TABLE_NAME    DDB — source of active tickers
    SEEN_CONTENT_TABLE_NAME         DDB — output (raw content + dedup)
    DATA_FEED_SECRET_ARN            astra/data-feed-credentials (finnhub_api_key)
    REDDIT_SECRET_ARN               astra/reddit-credentials (PRAW OAuth)
    SUBREDDITS                      comma-separated; default
                                    "wallstreetbets,options,stocks"
    TICKER_REFRESH_SECONDS          default 900 (15 min)
    INGESTION_TTL_DAYS              default 3
    FINNHUB_WS_URL                  default "wss://ws.finnhub.io"
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
import praw
import websocket  # websocket-client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("ingestion")

EARNINGS_TABLE = os.environ["EARNINGS_CALENDAR_TABLE_NAME"]
SEEN_TABLE = os.environ["SEEN_CONTENT_TABLE_NAME"]
DATA_FEED_SECRET_ARN = os.environ["DATA_FEED_SECRET_ARN"]
REDDIT_SECRET_ARN = os.environ["REDDIT_SECRET_ARN"]
SUBREDDITS = [s.strip() for s in os.environ.get("SUBREDDITS", "wallstreetbets,options,stocks").split(",") if s.strip()]
TICKER_REFRESH_SECONDS = int(os.environ.get("TICKER_REFRESH_SECONDS", "900"))
INGESTION_TTL_DAYS = int(os.environ.get("INGESTION_TTL_DAYS", "3"))
FINNHUB_WS_URL = os.environ.get("FINNHUB_WS_URL", "wss://ws.finnhub.io")

_dynamodb = boto3.resource("dynamodb")
_secretsmanager = boto3.client("secretsmanager")
_seen_table = _dynamodb.Table(SEEN_TABLE)
_earnings_table = _dynamodb.Table(EARNINGS_TABLE)

_active_tickers: set[str] = set()
_active_tickers_lock = threading.Lock()

_finnhub_key_cache: str | None = None


def _finnhub_key() -> str:
    global _finnhub_key_cache
    if _finnhub_key_cache is None:
        secret = _secretsmanager.get_secret_value(SecretId=DATA_FEED_SECRET_ARN)
        _finnhub_key_cache = json.loads(secret["SecretString"]).get("finnhub_api_key")
        if not _finnhub_key_cache:
            raise RuntimeError("finnhub_api_key missing in DataFeedCredentialsSecret")
    return _finnhub_key_cache


def _reddit_client() -> praw.Reddit:
    secret = _secretsmanager.get_secret_value(SecretId=REDDIT_SECRET_ARN)
    creds = json.loads(secret["SecretString"])
    return praw.Reddit(
        client_id=creds["client_id"],
        client_secret=creds["client_secret"],
        username=creds.get("username"),
        password=creds.get("password"),
        user_agent=creds.get("user_agent", "astra-ingestion/1.0"),
        check_for_async=False,
    )


def _hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def _ttl_epoch() -> int:
    return int((datetime.now(timezone.utc) + timedelta(days=INGESTION_TTL_DAYS)).timestamp())


def _write_mention(content_hash: str, ticker: str, source: str, content: str, url: str | None = None) -> None:
    """Conditional put on content_hash — dedup is free."""
    try:
        _seen_table.put_item(
            Item={
                "content_hash": content_hash,
                "ticker": ticker,
                "source": source,
                "content": content[:5000],
                "url": url,
                "ingested_at": datetime.now(timezone.utc).isoformat(),
                "ttl": _ttl_epoch(),
            },
            ConditionExpression="attribute_not_exists(content_hash)",
        )
    except _dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        pass  # already seen
    except Exception:  # noqa: BLE001
        LOG.exception("write_mention failed for hash=%s", content_hash[:12])


# ─── ticker-refresh thread ──────────────────────────────────────────────────


def _refresh_active_tickers() -> set[str]:
    today = datetime.now(timezone.utc).date()
    tomorrow = today + timedelta(days=1)
    targets = {today.isoformat(), tomorrow.isoformat()}
    tickers: set[str] = set()
    # earnings_calendar is keyed by date+ticker — scan a small window.
    resp = _earnings_table.scan(
        FilterExpression="target_T IN (:t0, :t1)",
        ExpressionAttributeValues={":t0": today.isoformat(), ":t1": tomorrow.isoformat()},
    )
    for item in resp.get("Items", []):
        t = item.get("ticker")
        if t:
            tickers.add(t)
    LOG.info("Active tickers refreshed: %s (target_T in %s)", sorted(tickers), sorted(targets))
    return tickers


def _refresh_tickers_loop() -> None:
    global _active_tickers
    while True:
        try:
            tickers = _refresh_active_tickers()
            with _active_tickers_lock:
                _active_tickers = tickers
        except Exception:  # noqa: BLE001
            LOG.exception("ticker refresh failed")
        time.sleep(TICKER_REFRESH_SECONDS)


# ─── Finnhub news WebSocket thread ──────────────────────────────────────────


def _news_ws_loop() -> None:
    """Connect to Finnhub WS, subscribe to active tickers, dedup-write news."""
    while True:
        try:
            with _active_tickers_lock:
                subbed = set(_active_tickers)
            if not subbed:
                time.sleep(30)
                continue

            url = f"{FINNHUB_WS_URL}?token={_finnhub_key()}"
            ws = websocket.create_connection(url, timeout=30)
            LOG.info("Finnhub WS connected; subscribing to %d tickers", len(subbed))
            for t in subbed:
                ws.send(json.dumps({"type": "subscribeNews", "symbol": t}))

            last_resub = time.time()
            while True:
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not raw:
                    break
                msg = json.loads(raw)
                if msg.get("type") != "news":
                    continue
                for item in msg.get("data") or []:
                    # Finnhub news payload: { headline, summary, source, url, related, datetime, ... }
                    related = (item.get("related") or "").upper().split(",")
                    for ticker in related:
                        if ticker in subbed:
                            content = f"{item.get('headline', '')}\n{item.get('summary', '')}"
                            h = _hash("news", item.get("url") or "", ticker)
                            _write_mention(h, ticker, "news", content, url=item.get("url"))

                # Re-check active tickers periodically so the subscription
                # follows the rolling earnings window.
                if time.time() - last_resub > TICKER_REFRESH_SECONDS:
                    with _active_tickers_lock:
                        latest = set(_active_tickers)
                    new = latest - subbed
                    gone = subbed - latest
                    for t in new:
                        ws.send(json.dumps({"type": "subscribeNews", "symbol": t}))
                    for t in gone:
                        ws.send(json.dumps({"type": "unsubscribeNews", "symbol": t}))
                    subbed = latest
                    last_resub = time.time()
        except Exception:  # noqa: BLE001
            LOG.exception("Finnhub WS loop crashed; reconnecting in 30s")
            time.sleep(30)


# ─── Reddit poller thread ───────────────────────────────────────────────────


def _reddit_loop() -> None:
    while True:
        try:
            with _active_tickers_lock:
                tickers = set(_active_tickers)
            if not tickers:
                time.sleep(60)
                continue

            reddit = _reddit_client()
            # Stream submissions from each subreddit; skip stale ones (older
            # than 24h) to bound the catchup on startup.
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            for sub_name in SUBREDDITS:
                LOG.info("Polling r/%s for %d tickers", sub_name, len(tickers))
                try:
                    sub = reddit.subreddit(sub_name)
                    for submission in sub.new(limit=200):
                        created = datetime.fromtimestamp(submission.created_utc, tz=timezone.utc)
                        if created < cutoff:
                            break
                        body = f"{submission.title}\n{getattr(submission, 'selftext', '')}"
                        upper = body.upper()
                        for ticker in tickers:
                            if f"${ticker}" in upper or f" {ticker} " in upper or upper.startswith(f"{ticker} "):
                                h = _hash("reddit", submission.id, ticker)
                                _write_mention(h, ticker, f"reddit/{sub_name}", body, url=f"https://reddit.com{submission.permalink}")
                except Exception:  # noqa: BLE001
                    LOG.exception("subreddit %s poll failed", sub_name)
            time.sleep(120)  # poll cadence — gentle on the Reddit API
        except Exception:  # noqa: BLE001
            LOG.exception("Reddit loop crashed; restart in 60s")
            time.sleep(60)


def main() -> None:
    LOG.info("NewsRedditIngestion starting")
    threads = [
        threading.Thread(target=_refresh_tickers_loop, name="tickers", daemon=True),
        threading.Thread(target=_news_ws_loop, name="news-ws", daemon=True),
        threading.Thread(target=_reddit_loop, name="reddit", daemon=True),
    ]
    for t in threads:
        t.start()
    # Keep main thread alive — daemons exit when this returns.
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
