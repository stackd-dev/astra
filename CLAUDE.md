# CLAUDE.md — Earnings Options Trading System

> Auto-loaded by Claude Code every session in this repo. This is the lean rules-and-decisions
> file. The detailed per-component reference (specs, data contracts, schemas, full clock) is
> `docs/earnings-system-design.md` — read it before building any component.

---

## 1. What this project is

A semi-automated **earnings options trading system**. It researches upcoming earnings, auto-
enters options positions the afternoon before the announcement, holds one overnight, and alerts
a human at the next market open to exit. **Entry is automated; exit is manual.**

This is a **ground-up rewrite** that reuses the prior `astra` repo as a *reference
architecture* (validated patterns to keep), NOT a codebase to extend. See §3.

---

## 2. The core trade pattern (read this carefully — it drives everything)

**ONE pattern: enter the afternoon before → hold exactly ONE overnight → exit at the next open.**
There is NO same-day intraday path. Every position is an overnight hold. Two timing variants of
the same pattern:

- **AMC (reports after market close on day T):** enter day T in the final ~30 min before the
  1pm PT close. Report drops T evening (~1:15–4:30pm PT); stock moves in after-hours. That
  after-hours move is a **PREVIEW signal only** — you CANNOT exit (no retail options after-
  hours). The monitor logs it and rolls it into the morning exit alert. Exit T+1 at the open.
- **BMO (reports before market open):** enter the prior afternoon. Report drops pre-open; exit
  that same open. No after-hours preview — react cold at the bell.

Same `hold_until` rule for both: **the next market open after entry.** ENTRY IS ALWAYS THE
AFTERNOON BEFORE — do not build a morning-entry path. Positions carry `report_timing`
(AMC/BMO) + `hold_until`.

This is the *enter-before-the-announcement* strategy: you hold through the report and eat both
the move AND the IV crush. It is the high-variance version. The owner has run this manually over
months and is confident in the edge; validation before live money is via RECOMMENDATION-ONLY
MODE on the real system against live data (see §12), not historical backtesting.

## 3. Reuse policy — astra is a reference, not a base

REWRITE from scratch. Use astra's validated decisions as guidelines; discard MVP-expedient or
news-alerter-shaped code.

**Preserve (validated):** persistent WebSocket → SQS buffer → processor pattern with auto-
reconnect; content-hash dedup in DynamoDB with TTL; Secrets-Manager-everything (no keys in
code/env); modular stacks with clean dependency boundaries; all-serverless compute (except the
one EC2 box, §6).

**Redesign:** the old Data/Ingest/Processor layout (re-org around the trading pipeline, §5);
the hardcoded NVIDIA/OpenAI regex filter (replace with a general feed processor); Slack-as-
destination (alerts now go to SMS).

## 4. Tech stack (decided)

- **Infrastructure: AWS CDK in TypeScript.** Same IaC paradigm as astra; keep it.
- **Business logic: Python** for Lambda/Fargate tasks doing data/quant work (sentiment, scoring,
  backtesting, options math) — better libraries (pandas, numpy, options tooling). CDK (TS) can
  define Lambdas with a Python runtime. So: TS for infra, Python for logic.
- **Broker API:** `ib_insync` (Python) talking to IB Gateway (§6).
- **WebSocket feed listener:** rebuild cleanly (don't copy astra's) as a general feed listener —
  preserve the pattern, not the implementation.

## 5. Stack layout — THREE stacks only

Boundaries follow deployment lifecycle and blast radius, NOT the pipeline diagram. Do not split
further without strong reason.

- **CoreStack** — shared, long-lived, STATEFUL infra: DynamoDB tables, SQS, SNS, Secrets, S3
  config bucket (holds the STOP kill-switch file), EBS volume for the EC2 box. Deployed once,
  rarely touched, highest blast radius. Everything depends on it.
- **PipelineStack** — the entire stateless research-and-decision "brain": all data fetchers
  (earnings calendar, options chains, news WebSocket, Reddit/social), Bedrock sentiment, AND the
  strategy/scoring engine. NO money-at-risk code here — iterate freely.
- **ExecutionStack** — everything that touches the broker and real money: IB Gateway on the EC2
  box, order executor, position monitor. Isolated deliberately: only stack that can place trades,
  so its IAM, deploys, and blast radius are walled off. BUILD LAST, deploy most carefully.

Rationale: (1) stateful core vs. stateless compute — never let a routine logic deploy risk
DynamoDB/EBS; (2) research vs. execution — money-losing code is categorically different, isolate
it. Sentiment and strategy do NOT get separate stacks (tuned as one unit, never deploy
independently). PipelineStack and ExecutionStack consume CoreStack's resources, not duplicates.

## 6. IB Gateway & hosting (decided)

IB Gateway is IBKR's headless API connection app — the always-on local process that holds the
authenticated link to Interactive Brokers. Your `ib_insync` code connects to it over localhost.
It cannot be serverless (stateful, must stay logged in).

- **Runs on a single 24/7 EC2 instance** (t3.small). IBC for auto-login, a systemd unit to keep
  it alive, EBS for session/login files. This is the **SINGLE exception** to an all-serverless
  system — do NOT pile other services onto it.
- IBKR forces a daily restart (~midnight). The monitor MUST survive it and re-read open
  positions on every restart so an overnight hold is never orphaned (worst-case failure).
- Why EC2 not Fargate: cheaper at steady-state, SSH-debuggable when IB Gateway wedges, and
  overnight holds require always-on. (Fargate was the prior pick; switched deliberately.)
- **NAT Gateway is a hidden cost** (~$32/mo + $0.045/GB). Put the EC2 box in a public subnet
  with a tight security group OR budget the NAT Gateway explicitly. Can exceed compute cost.

## 7. Strategy logic

- **Edge signal:** per candidate, implied move from the ATM straddle vs. historical actual post-
  earnings move (~last 8 quarters). Edge ratio = historical_move / implied_move; >~1.2 means the
  ticker tends to move more than priced in — a long-vol candidate.
- **Direction** (calls vs puts) is hard and near coin-flip. Use a confidence threshold; skip
  below it; straddle when ambiguous. Don't over-trust directional signals.
- **Scoring formula** (weights TUNABLE — start from the owner's manual heuristics, refine from
  recommendation-only review results; never hardcode arbitrary guesses):
  `score = w1*edge_ratio + w2*direction_confidence + w3*liquidity - w4*spread_penalty + w5*sentiment`
- **Contract:** slightly-OTM weekly expiring the Friday after earnings, ~0.30–0.40 delta.
- **Sizing:** max loss per trade = 25–33% of per-trade capital (capital range $2k–$10k). Earnings
  options regularly go to zero — size accordingly.

## 8. Sentiment layer

- **Amazon Bedrock, model `claude-haiku-4-5`.** NOT Comprehend, NOT VADER as primary — both fail
  on WSB slang/sarcasm. Bedrock stays in AWS/IAM, no separate key. VADER only as a fallback if
  the Bedrock call fails, so the scorer always has some signal.
- **JSON-only output**, fixed schema: sentiment_score, conviction, volume_signal,
  is_contrarian_setup, key_themes, noise_ratio. No preamble/markdown.
- **Sentiment is a tiebreaker, not a trigger.** Start w5 low; raise it only if recommendation-
  only review shows it adds signal. Social sentiment is often noise after costs.
- Use Bedrock **batch + prompt caching** for the nightly run (identical system prompt every call;
  not real-time, so the batch window is fine).

## 9. Hard rules

1. AI extracts features (sentiment); **deterministic math makes the trade decision.** Never let
   an LLM decide a trade.
2. Infra in CDK/TypeScript; data/quant logic in Python.
3. Everything serverless EXCEPT the single IB Gateway EC2 box.

## 10. Safety rails (hardcode, not config)

- `STOP` file in the S3 config bucket: if it exists, the executor places NO trades. Kill switch.
- Max N trades/day; max total $/day deployed; per-ticker position cap.
- Reject any order > 2x average historical fill size (sanity check).
- Idempotency: tag every order with a trade_id so a reconnect never double-places.
- Monitor dead-man's-switch: if it can't reconnect / hasn't checked in for N minutes, alert the
  human. An unmonitored open overnight position is the worst case.
- Start live at 1 contract; scale only after 20+ clean live trades. Run in recommendation-only
  mode (§12) before any live dollar.

## 11. Cost constraints

- Ongoing infra budget ~**$100/month.** AWS core + new pieces stay well under it; Bedrock
  sentiment is pennies/mo; SMS ~$10. Watch the NAT Gateway (§6).
- **Live market data** is the main external cost: a real-time/near-real-time options-chain feed
  for the nightly OptionsChainFetcher (implied move, IV, chains). Options: the IBKR data feed
  itself, Finnhub, or Polygon. Pick the cheapest source that provides ATM straddle pricing + IV
  for the candidate universe. No historical-data purchase is needed (no backtesting).
- Build the pipeline on free/cheap data first (yfinance, free Finnhub tier), then subscribe to a
  paid live feed only when moving toward recommendation-only / live trading.

## 12. Build sequence (validation via recommendation-only mode — do NOT jump to live money)

No backtester. Validation is forward: build the real system, run it live but in recommendation-
only mode, review the calls against real market opens, then flip to live money once confirmed.

1. **Build the real infrastructure.** CoreStack, then PipelineStack with the real analyzers
   (fetchers → sentiment → strategy/scoring), then ExecutionStack (EC2 + IB Gateway + executor +
   monitor). Fully functional, connected to live data.
2. **Recommendation-only mode (THE GATE).** The system runs the full nightly + entry cycle but
   places NO orders. Instead, at the entry window it records the exact trade it WOULD make
   (ticker, contract, side, size, entry price, reasoning/scores) to `pending_trades` /
   `trade_log` and alerts the owner. This is a config flag on the OrderExecutor — same code path
   as live, minus the order submission.
3. **Daily review loop.** Each morning at the open, the owner reviews the recommended trades
   against the actual market move (and the AMC after-hours preview where present) to judge
   whether each call was good. Results are brought to a review session (with Claude / Claude Code)
   to evaluate the system's picks — which signals predicted well, which were noise. The owner
   gates progression: enough good recommendations → proceed; not good enough → fix the analyzers
   and keep recommending.
4. **Flip to live, tiny.** Once the owner confirms the recommendations are sound, flip the
   OrderExecutor config from recommendation-only to live. Start at 1 contract; scale only after
   20+ clean live trades.

(Later scope — NOT in the current build: a deliberate, human-gated improvement loop that
periodically reviews accumulated live results and adjusts weights/logic. Deliberately periodic,
never auto-retuning per-trade — small samples are noise and per-trade retuning overfits.)

## 13. Explicitly rejected (don't re-propose)

- Extending old astra code (rewrite; reference only).
- Fargate for IB Gateway (decided 24/7 EC2 — §6). Keep everything ELSE serverless; EC2 is the
  single exception.
- Scheduled start/stop of the EC2 box (overnight holds require always-on).
- Comprehend / VADER as primary sentiment (Bedrock Haiku; VADER fallback only).
- Fully automated EXIT (manual is intentional).
- A morning-entry path (entry is always the afternoon before).
- Robinhood / Webull / Schwab as broker (no serious automation API). **Use Interactive Brokers**
  (Tastytrade distant second). Account not yet opened — needs IBKR Pro + options Level 3 + API.
- LLM making the trade decision.

## 14. Owner to-dos (have external lead time — start now)

- Open IBKR account, apply options Level 3, enable API access (2–3 week lead).
- Pick the live options-data feed source (IBKR feed / Finnhub / Polygon) for the chain fetcher.
- Decide NAT Gateway vs. public-subnet placement for the EC2 box.

---

*Not financial advice. Earnings options strategies frequently have negative expectancy after
spreads and commissions — especially the enter-before-announcement variant used here, which eats
the full IV crush. Recommendation-only mode against live data is the gate before risking real
money.*
