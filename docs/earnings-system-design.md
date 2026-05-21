# Earnings Options Trading System — Design Doc

> Companion to `CLAUDE.md` (the lean, always-loaded rules file). This doc is the
> read-on-demand reference: per-component specs, data contracts, schemas, and the
> two operating modes. Read the relevant section before building a component.

---

## 0. System purpose & the core trade pattern

The system researches upcoming earnings events, auto-enters options positions the afternoon
before the announcement, holds exactly one overnight, and surfaces exit decisions to a human
at the next market open (exit is manual).

**There is ONE trade pattern, not multiple modes: enter PM → hold one overnight → exit at the
next open.** Every position is an overnight hold. There is NO same-day intraday path. The only
variation is *when the announcement lands during the single overnight*:

- **AMC (after-market-close) tickers:** report AMC on day T. Enter day T afternoon (final ~30
  min before the 1pm PT close). Report drops T evening (~1:15–4:30pm PT). Stock moves in
  after-hours — **this after-hours move is a PREVIEW signal, not an exit opportunity** (retail
  cannot trade options after-hours). Exit T+1 at the open. The monitor ingests the after-hours
  print and rolls it into the morning exit alert.
- **BMO (before-market-open) tickers reporting Tue morning:** enter Mon afternoon. Report drops
  Tue pre-open (~4:00–6:00am PT). Exit Tue at the open. No after-hours preview — you react cold
  at the open.

Both share identical mechanics and the same `hold_until` rule: **the next market open after
entry.** AMC gives a preview the evening of entry; BMO does not.

**Position lifecycle = always overnight.** Every position row carries `report_timing`
(AMC/BMO) and `hold_until` (next open). The monitor MUST persist across the overnight, survive
IB Gateway's daily restart, and re-read open positions on every restart so a held position is
never orphaned. A held position that loses monitoring is the worst-case failure — recover open
positions from state on every restart, and alert the human if the monitor can't reconnect.

---

## 1. Hosting model (decided)

- **IB Gateway: a single 24/7 EC2 instance** (t3.small, ~2 vCPU / 2GB). Reasons: cheaper than
  Fargate at steady-state, SSH-debuggable when IB Gateway wedges, and overnight-hold
  flexibility requires always-on. Use IBC for auto-login, a systemd unit to keep IB Gateway
  alive, and an EBS volume for session/login files. This box is the **single exception** to
  the otherwise-serverless system — do NOT pile general-purpose services onto it.
- **Everything else: serverless / ephemeral.** Data fetchers, sentiment, and strategy are
  scheduled Lambdas or on-demand Fargate tasks. They wake, work, write, and die.
- **NAT Gateway warning:** the EC2 box and any private-subnet Lambdas making outbound calls
  incur NAT Gateway cost (~$32/mo base + $0.045/GB). Either put the EC2 box in a public subnet
  with a tight security group, or budget for the NAT Gateway explicitly. This can exceed the
  compute cost — design around it.

---

## 2. Timelines — the full clock

The unifying shape: **enter the afternoon before, hold one overnight, exit at the next open.**

### Nightly research cycle (~8:00pm PT, T-1, scheduled, all serverless, ephemeral)
```
EarningsFetcher
   → pulls tickers reporting AMC on T AND BMO on T+1 morning
     (both are "enter T afternoon" candidates)
   → OptionsChainFetcher (per candidate) → chains + implied move + edge ratio
   → NewsRedditIngestion (per candidate)  → raw posts/headlines
   → SentimentProcessor (Bedrock batch)   → sentiment scores
   → StrategyEngine (dry-run or live)      → ranked trades to pending_trades,
        each tagged report_timing (AMC/BMO) + hold_until (next open after entry)
```
All ephemeral. If a step fails, downstream uses what's available (sentiment is optional; the
scorer runs without it at lower confidence). If EarningsFetcher fails, no candidates → no
trades → fail safe.

### Entry — TRADING DAY T afternoon (EC2 box, IB Gateway live)
```
~12:30–12:55pm PT (final ~30 min before the 1pm PT close)
  OrderExecutor reads pending_trades
   → checks STOP file + safety rails
   → ENTERS positions for BOTH:
       • AMC tickers reporting after today's (T) close
       • BMO tickers reporting before tomorrow's (T+1) open
     Both entered NOW, before their respective announcements.
   → writes fills to live_positions (report_timing, hold_until = next open)
1:00pm PT: market closes
```
Entry window is the same for AMC and BMO — the afternoon before the move. Do NOT build a
morning-entry path; entry is always the prior afternoon.

### Overnight — announcement lands, monitor persists
```
AMC: report drops T evening (~1:15–4:30pm PT). Stock moves after-hours.
  PositionMonitor captures the after-hours print as a PREVIEW of the T+1 open.
  CANNOT exit (no retail options after-hours) — preview signal only.
BMO: report drops T+1 pre-open (~4:00–6:00am PT). No preview; react at the open.

PositionMonitor persists across the overnight and the IB Gateway daily restart.
On every restart it RE-READS open positions from live_positions — never orphan a held
position. Dead-man's-switch: if the monitor can't reconnect, alert the human immediately.
```

### Exit — next market OPEN (T+1 for AMC, Tue open for the BMO example)
```
6:30am PT: market opens. The move + IV crush hit.
  PositionMonitor computes P&L%, IV change, stabilization signal.
   → fires SMS exit alerts. For AMC, the alert includes the after-hours preview
     context ("moved +6% after-hours, opening now, projected P&L ...").
6:30am–~8:30am PT:
  HUMAN exits manually from the IBKR mobile app.
  Exits logged to trade_log for the analyzer.
```

---

## 3. Components

Each component below: **Trigger · Inputs · Processing · Outputs · Compute · Failure behavior ·
Handoff.**

### 3.1 EarningsFetcher
- **Trigger:** scheduled nightly ~8:00pm PT (EventBridge), on day T-1 (the evening before the
  trading day on which entry happens).
- **Inputs:** external earnings-calendar API (Finnhub/Polygon), with each event's AMC/BMO flag.
- **Processing:** select the tickers that are "enter tomorrow (T) afternoon" candidates —
  i.e. those reporting **AMC on T** OR **BMO on T+1 morning**. Both groups are entered the
  same afternoon (T). Tag each with `report_timing`. Filter to liquid optionable names (avg
  daily volume > 1M, market cap > $2B, has weeklies). Dedup via content hash. (Getting the
  AMC/BMO flag right is critical — it determines whether an after-hours preview will exist.)
- **Outputs:** candidate tickers + `report_timing` to `earnings_calendar` (DynamoDB).
- **Compute:** Lambda (Python), seconds to run.
- **Failure:** if the API is down, log and alert; no candidates → no trades next afternoon
  (fail safe).
- **Handoff:** OptionsChainFetcher reads `earnings_calendar`.

### 3.2 OptionsChainFetcher
- **Trigger:** chained after EarningsFetcher (nightly).
- **Inputs:** candidate tickers from `earnings_calendar`; live options-chain API (IBKR feed /
  Finnhub / Polygon).
- **Processing:** for each candidate, pull the chain (strikes, IV, OI, volume, bid/ask).
  Compute implied move from the ATM straddle: `(call_mid + put_mid) / spot`. Pull last ~8
  quarters of actual post-earnings moves for the edge ratio.
- **Outputs:** chain snapshot to S3 (Parquet, partitioned by date/ticker); implied move +
  historical moves to `historical_moves` (DynamoDB).
- **Compute:** Lambda (Python), or Fargate task if chains are large.
- **Failure:** per-ticker; one ticker failing doesn't block others. Tickers without chain
  data are dropped from the candidate set.
- **Handoff:** StrategyEngine reads implied move + historical moves. Chain snapshots are
  archived to S3 for later analysis (e.g. reviewing why a trade was picked).

### 3.3 NewsRedditIngestion
- **Trigger:** two parts. (a) real-time news WebSocket listener — a long-lived feed listener
  pushing to SQS (preserve Astra's pattern, rebuild clean). (b) nightly Reddit scrape per
  candidate (PRAW).
- **Inputs:** Finnhub news WebSocket; Reddit (r/wallstreetbets, r/options, r/thetagang,
  r/stocks); candidate tickers.
- **Processing:** match posts/headlines to tickers via cashtag ($TICKER) + company name (NOT
  bare symbols — avoids collisions like "ON"). Dedup via content hash with TTL. Compute
  mention velocity (today vs 7-day baseline).
- **Outputs:** raw posts/headlines + velocity to a staging table or SQS for the sentiment
  processor.
- **Compute:** WebSocket listener = long-lived task; Reddit scrape = nightly Lambda.
- **Failure:** sentiment is optional downstream — if ingestion fails, the scorer runs without
  it. Never block trading on social data.
- **Handoff:** SentimentProcessor consumes staged text.

### 3.4 SentimentProcessor
- **Trigger:** chained after ingestion (nightly).
- **Inputs:** staged posts/headlines per ticker.
- **Processing:** batch posts per ticker, call **Amazon Bedrock `claude-haiku-4-5`** with a
  cached system prompt (instructions + slang glossary + JSON schema). Use Bedrock **batch**
  (nightly, not real-time, so the async window is fine) + **prompt caching**. Force JSON-only
  output. Separate Reddit-derived from news-derived sentiment in storage (different quality;
  weighted independently later).
- **Outputs:** per-ticker structured sentiment to `sentiment_scores`:
  `{sentiment_score, conviction, volume_signal, is_contrarian_setup, key_themes, noise_ratio}`.
- **Compute:** Lambda (Python) orchestrating Bedrock batch.
- **Failure:** fall back to VADER for a coarse score so the scorer always has *something*.
  Sentiment weight starts low regardless.
- **Handoff:** StrategyEngine reads `sentiment_scores` as the w5 term.

### 3.5 StrategyEngine (the scorer)
- **Trigger:** chained after sentiment (nightly). Two run modes: **dry-run** (log trades it
  WOULD make, no execution) and **live** (write executable orders). Start in dry-run.
- **Inputs:** `historical_moves` (edge ratio), chain data, `sentiment_scores`, config weights
  from S3 (`strategy.yaml`).
- **Processing:** per candidate compute edge ratio = historical_move / implied_move (>~1.2 is
  a long-vol candidate). Determine direction (calls/puts) with a confidence threshold; skip
  below it; straddle when ambiguous. Select contract (slightly-OTM weekly expiring the Friday
  after earnings, ~0.30–0.40 delta). Size position (max loss = 25–33% of per-trade capital).
  Score:
  `score = w1*edge_ratio + w2*direction_confidence + w3*liquidity - w4*spread_penalty + w5*sentiment`
  Weights are TUNABLE — seed from the owner's manual heuristics, refine from recommendation-only
  review results. Never hardcode arbitrary guesses.
- **Outputs:** ranked executable orders to `pending_trades` (DynamoDB), each with
  `report_timing` (AMC/BMO) and `hold_until` (the next market open after entry — same rule for
  both AMC and BMO).
- **Compute:** Lambda (Python).
- **Failure:** if it errors, no `pending_trades` written → executor places nothing (fail safe).
- **Handoff:** OrderExecutor reads `pending_trades`.

### 3.6 OrderExecutor
- **Trigger:** scheduled on trading day T, in the **final ~30 min before the 1pm PT close
  (~12:30pm PT)** — NOT pre-market. Entry is always the afternoon before the move, for both
  AMC (reporting T after-close) and BMO (reporting T+1 pre-open). Runs on the EC2 box.
- **Inputs:** `pending_trades`; the STOP file in S3; safety-rail config.
- **Processing:** FIRST check `s3://<config>/STOP` — if it exists, place NO trades. Enforce
  rails: max N trades/day, max total $/day, per-ticker cap, reject orders > 2x avg historical
  fill size. **MODE FLAG (`execution_mode` in config): `recommend` vs `live`.** In `recommend`
  mode (the validation gate), run the entire path EXCEPT the order submission — compute the exact
  trade (ticker, contract, side, size, intended entry price, reasoning/scores), record it to
  `pending_trades`/`trade_log`, and alert the owner, but place NO order. In `live` mode, for each
  approved trade: submit limit at mid → if unfilled, cancel + resubmit at mid+1 tick → if still
  unfilled near the close, take it or skip (config flag). Must complete before the 1pm PT close —
  there is no entry after close. Connects via `ib_insync`. The two modes share one code path so
  what gets validated in `recommend` is exactly what runs in `live`.
- **Outputs:** fills to `live_positions` (report_timing, entry price/time, qty, hold_until);
  all events to `trade_log`.
- **Compute:** Python service on the EC2 box (shares IB Gateway connection).
- **Failure:** partial fills logged; connection loss → retry, then alert + abort (never
  double-submit). Idempotency: tag each order with a trade_id so reconnect doesn't re-place.
  If entry can't complete before close, skip the trade (do NOT carry to next day — the setup
  is gone).
- **Handoff:** PositionMonitor reads `live_positions`.

### 3.7 PositionMonitor
- **Trigger:** starts at entry (T afternoon) and PERSISTS through the overnight until the
  position is closed at the next open. Every position is an overnight hold. Polls every ~30s
  during the relevant windows.
- **Inputs:** `live_positions`; live + after-hours quotes via IB Gateway; current IV;
  `report_timing`.
- **Processing:**
  - **AMC after-hours (T evening):** capture the after-hours move as a PREVIEW signal of the
    T+1 open. Log it; do NOT alert to exit (retail can't trade options after-hours). Roll the
    preview into the morning exit alert.
  - **Overnight persistence:** survive the IB Gateway daily restart. **On every start/restart,
    re-read open positions from `live_positions`** so a held position is never orphaned. Run a
    dead-man's-switch: if the monitor can't reconnect / hasn't checked in for N minutes, alert
    the human immediately — an unmonitored open position is the worst case.
  - **At the next open (exit window):** compute P&L %, IV change since entry (IV crush is
    expected and large here), underlying 5-min std-dev (stabilization). Fire exit alerts on
    P&L thresholds (+25/+50/+100%, -50%), IV crush (>30% drop), stabilization, and time pings
    (open+5/30/120min). For AMC, include the after-hours preview context in the first alert.
- **Outputs:** SMS alerts (Twilio or SNS). Does NOT trade — human exits from IBKR mobile.
- **Compute:** Python service on the EC2 box (shares IB Gateway connection).
- **Failure:** if it dies, systemd restarts it and it recovers open positions from state.
  Alert the human if it can't reconnect — an unmonitored open position is the worst case.
- **Handoff:** human action; exits logged to `trade_log` for the analyzer.

---

## 4. Data contracts — DynamoDB tables & S3 paths

### DynamoDB
| Table | Key | Fields | Written by | Read by |
|---|---|---|---|---|
| `earnings_calendar` | date+ticker | report_timing(AMC/BMO), time, eps_estimate, liquidity_flags | EarningsFetcher | OptionsChainFetcher |
| `historical_moves` | ticker+quarter | implied_move, actual_move, edge_ratio | OptionsChainFetcher | StrategyEngine |
| `sentiment_scores` | ticker+date | sentiment_score, conviction, volume_signal, is_contrarian_setup, key_themes, noise_ratio, source(reddit/news) | SentimentProcessor | StrategyEngine |
| `pending_trades` | trade_id | ticker, contract, side, qty, limit_price, report_timing, hold_until, status | StrategyEngine | OrderExecutor |
| `live_positions` | position_id | contract, entry_price, entry_time, qty, report_timing, hold_until, after_hours_preview | OrderExecutor (writes), PositionMonitor (updates preview) | PositionMonitor |
| `trade_log` | trade_id+ts | event_type, payload_json | all execution components | Analyzer |
| `seen_content` | content_hash | ttl | ingestion | ingestion (dedup) |

### S3
| Path | Contents | Written by | Read by |
|---|---|---|---|
| `options-chains/<date>/<ticker>.parquet` | full chain snapshot | OptionsChainFetcher | Analyzer / human review |
| `config/STOP` | kill switch (existence = halt) | human | OrderExecutor |
| `config/strategy.yaml` | tunable weights w1..w5, thresholds | human (refined from recommendation-only review) | StrategyEngine |
| `logs/<date>/` | replicated logs | all | Analyzer |

---

## 5. Build sequence (validation via recommendation-only mode — no backtester)

Build the real system, run it live in recommendation-only mode, review against real opens, then
flip to live money once the owner confirms.

1. **Build the real infrastructure.** CoreStack → PipelineStack (real analyzers: fetchers →
   sentiment → strategy/scoring) → ExecutionStack (EC2 + IB Gateway + executor + monitor).
   Fully functional on live data.
2. **Recommendation-only mode (THE GATE).** OrderExecutor runs with `execution_mode=recommend`:
   the full nightly + entry cycle runs but places NO orders — it records the exact trade it would
   make (contract, side, size, entry price, scores/reasoning) and alerts the owner. Same code
   path as live, minus submission. Test BOTH the AMC path (enter T PM → after-hours preview →
   exit T+1 open) and the BMO path. Verify the monitor survives the IB Gateway daily restart and
   recovers open positions.
3. **Daily review loop.** Each morning the owner reviews the recommended trades against the
   actual open (and AMC after-hours preview) to judge each call, and brings results to a review
   session (Claude / Claude Code) to evaluate which signals predicted well vs. noise. Owner gates
   progression.
4. **Flip to live, tiny.** Owner flips `execution_mode` to `live`. Start at 1 contract; scale
   after 20+ clean live trades.

(Later scope, NOT in this build: a deliberate, human-gated periodic improvement loop that
reviews accumulated live results and adjusts weights/logic. Never auto-retune per-trade — small
samples are noise and per-trade retuning overfits.)

---

## 6. Open items
- Open IBKR account, options Level 3, API access (2–3 wk lead — start now).
- Pick the live options-data feed (IBKR feed / Finnhub / Polygon) for the chain fetcher.
- Decide NAT Gateway vs public-subnet placement for the EC2 box.

---

*Not financial advice. Earnings options strategies frequently have negative expectancy after
spreads and commissions. Recommendation-only mode against live data is the gate before real money.*
