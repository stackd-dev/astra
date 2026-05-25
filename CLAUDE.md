# CLAUDE.md — Earnings Options Trading System

> Auto-loaded by Claude Code every session in this repo. This is the lean rules-and-decisions
> file. The detailed per-component reference (specs, data contracts, schemas, full clock) is
> `docs/earnings-system-design.md` — read it before building any component.

---

## 1. What this project is

A semi-automated **earnings options trading system**. It researches upcoming earnings, auto-
enters options positions the afternoon before the announcement, holds one overnight, and alerts
a human at the next market open to exit. **Entry is automated; exit is manual.**

This is a **ground-up rewrite** that reuses the prior `astra` repo as a _reference
architecture_ (validated patterns to keep), NOT a codebase to extend. See §3.

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

This is the _enter-before-the-announcement_ strategy: you hold through the report and eat both
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
- **Python Lambda packaging:** any Python Lambda with pip deps uses CDK Docker bundling
  (`Code.fromAsset` with `bundling.image = Runtime.PYTHON_3_12.bundlingImage`). Local pip on
  macOS produces darwin wheels that fail at Lambda runtime — established the hard way via the
  `ib_async`/numpy incident on 2026-05-24. **Docker Desktop must be running** for `cdk
  synth`/`cdk deploy` of these stacks. Don't propose `--platform manylinux2014_x86_64`
  workarounds or local bundling. When we have 3+ Python Lambdas sharing deps, revisit Poetry
  + Lambda Layers as a shared-deps optimization.
- **All Python Lambdas in this repo are ARM64** (`Architecture.ARM_64`, Graviton2). ~20%
  cheaper than x86_64 and avoids Docker-on-Apple-Silicon arch mismatches during bundling
  (without this, Docker pulls the arm64 build image but Lambda is x86_64 → import errors).
  When bundling, pin `platform: "linux/arm64"` on the bundling config so Docker pulls the
  arm64 variant of the SAM build image regardless of host arch.
- **Broker API:** `ib_async` (Python) talking to IB Gateway (§6). NOTE: use `ib_async`, NOT
  `ib_insync` — per IBKR's own docs, ib_insync is built on a legacy API release and is no longer
  maintained; ib_async is the modernized successor by an original ib_insync developer (same API,
  same patterns). Alternatively evaluate IBKR's new built-in Synchronous API (TWS 10.40+, Python-
  only) for the executor/monitor — it's simpler (linear top-to-bottom code, no callbacks/threads)
  and may cover our needs (connect, snapshot, place limit order, check status, cancel). See §6.
- **WebSocket feed listener:** rebuild cleanly (don't copy astra's) as a general feed listener —
  preserve the pattern, not the implementation.

## 5. Stack layout — TWO stacks (consolidated 2026-05-23)

The only boundary that matters for blast radius is **stateful vs stateless**. Money-safety
gets enforced at the construct level (STOP file, READ_ONLY_API hardware guard, recommend-mode
config flag, per-task IAM roles) — not at the stack boundary.

- **Astra-CoreStack** — shared, long-lived, STATEFUL infra: DynamoDB tables, SQS, SNS,
  Secrets Manager, S3 config bucket (holds the STOP kill-switch file). Deployed once, rarely
  touched. Highest blast radius; everything depends on it.
- **Astra-ComputeStack** — everything stateless: all data fetchers (earnings calendar,
  options chains, news WebSocket, Reddit/social), Bedrock sentiment, the strategy/scoring
  engine, **plus** the IB Gateway Fargate task, OrderExecutor, and PositionMonitor. Single
  VPC, single ECS cluster — Lambdas and Fargate tasks share a network so OptionsChainFetcher
  can reach the Gateway task internally without firewall gymnastics. Money-at-risk code lives
  alongside research code; isolation is enforced via per-task IAM, the STOP file, and the
  `READ_ONLY_API=yes` Gateway env (see §6).

Rationale: stateful vs stateless is the boundary that actually limits blast radius.
Previously had a third "ExecutionStack" for money-at-risk isolation; that turned out to be
over-engineering for a single-owner pre-live system — the safety rails that actually matter
don't require a separate stack. Revisit the split when going production-scale with multiple
developers or stricter compliance requirements. Sentiment + strategy were never going to get
separate stacks (tuned as one unit). ComputeStack consumes CoreStack's resources via props,
not duplicates.

## 6. IB Gateway & hosting (decided 2026-05-23)

IB Gateway runs as a **Fargate ECS service** using the community Docker image
**`gnzsnz/ib-gateway`** (pinned to `stable`). The image bundles IB Gateway + IBC + Xvfb +
socat + the auto-restart/re-auth logic. Your `ib_async` code connects to the Fargate task's
public IP over the socat-republished port. Cluster `astra-execution`, service `ib-gateway`.

- **Service: ECS Fargate**, 1 vCPU / 4 GB, single task, single AZ (us-east-1a). Roughly
  ~$42/mo. The cluster lives in a one-AZ VPC with a public subnet (no NAT Gateway — saves
  ~$32/mo). **EFS** persists `/home/ibgateway/tws_settings` across task replacements so
  session tokens survive (avoids 2FA on every restart). NOTE: do NOT mount EFS at
  `/home/ibgateway/Jts` — that's where the image bundles `jts.ini.tmpl` and other templates;
  mounting there shadows them and the container fails. Use `tws_settings` (a separate path)
  and set `TWS_SETTINGS_PATH` env var to match.
- **External connection ports** (after the image's socat layer): paper = **4004**,
  live = **4003**. *Internally* the Gateway listens on 4002/4001 (the canonical IB Gateway
  paper/live ports), but socat republishes them so external clients use 4004/4003. This is
  specific to the gnzsnz image — a bare Gateway install would use 4001/4002 directly.
- **Read-Only API is the hardware-level recommend-mode guard** — keep `READ_ONLY_API=yes`
  in the task env until you actually flip to live trading. Gateway rejects any order
  submission even if `execution_mode` in code says otherwise. Belt-and-suspenders with
  §12's recommend gate.
- **Daily restart + weekly re-auth:** image env vars `AUTO_RESTART_TIME=11:59 PM` and
  `RELOGIN_AFTER_TWOFA_TIMEOUT=yes` handle the daily restart and Saturday-night server-reset
  re-auth automatically. Paper accounts often skip 2FA entirely on API login. Live accounts
  will require an IBKR Mobile push at first login and after weekend resets — the monitor
  must alert when this is needed. Session-token persistence on EFS skips 2FA on routine
  task restarts (token tied to filesystem, not instance).
- Why Fargate, not EC2: previous attempt at EC2 + bare-metal IB Gateway + IBC required
  hours of debugging xterm dependencies, Xvfb access control, IBC's hardcoded `/opt/ibc`
  defaults, TWS_MAJOR_VRSN drift, exit code 1100 mysteries. The Docker image owns all of
  that. ~$12/mo cost delta vs t3.medium is the price of letting an actively-maintained
  community image handle the surface area. Audit the image's Dockerfile + scripts at
  [github.com/gnzsnz/ib-gateway-docker](https://github.com/gnzsnz/ib-gateway-docker) before
  going live.
- **Lambda → Gateway networking is internal** thanks to the two-stack consolidation (§5).
  OptionsChainFetcher and other Lambdas run in the same VPC as the Gateway Fargate task; they
  reach Gateway over its VPC-internal address (private IP / service-discovery name). No SG
  opening for external IPs, no NAT Gateway for stable egress.
- **API pacing limit:** IBKR's TWS API default is 50 requests/sec — well above strategy
  needs, but known.

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
  for the nightly OptionsChainFetcher (implied move, IV, chains). Preferred source: **IBKR's own
  options data via the API** (a small monthly market-data subscription, ~$5–15/mo for US options)
  — it makes the signal source and execution source the SAME, so the price you analyze matches
  the price you trade. Finnhub/Polygon are fallbacks, but Finnhub's option-chain ATM pricing has
  reported accuracy issues that would corrupt the implied-move signal. No historical-data
  purchase is needed (no backtesting).
- Build the pipeline on free/cheap data first (yfinance, free Finnhub tier), then subscribe to a
  paid live feed only when moving toward recommendation-only / live trading.

## 12. Build sequence (validation via recommendation-only mode — do NOT jump to live money)

No backtester. Validation is forward: build the real system, run it live but in recommendation-
only mode, review the calls against real market opens, then flip to live money once confirmed.

1. **Build the real infrastructure.** CoreStack (stateful), then ComputeStack (everything
   stateless: data fetchers → sentiment → strategy/scoring + IB Gateway Fargate + executor +
   monitor — all in one stack per §5). Fully functional, connected to live data.
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
- EC2 + bare-metal IB Gateway + IBC install (tried, abandoned 2026-05-23 — see §6 for the
  Fargate decision).
- Three-stack split (Core + Pipeline + Execution) — consolidated to two stacks on 2026-05-23
  per §5. Re-propose only if going production-scale with multiple developers / stricter
  compliance.
- Scheduled start/stop of the IB Gateway task (overnight holds require always-on).
- Comprehend / VADER as primary sentiment (Bedrock Haiku; VADER fallback only).
- Fully automated EXIT (manual is intentional).
- A morning-entry path (entry is always the afternoon before).
- Robinhood / Webull / Schwab as broker (no serious automation API). **Use Interactive Brokers
  Pro** (Tastytrade distant second). Account is OPEN and APPROVED with options + API access.
- LLM making the trade decision.

## 14. Owner to-dos

- [DONE] IBKR Pro account opened, approved, options + API access.
- [DONE 2026-05-23] IB Gateway running as Fargate task in paper mode; `ib_async` confirmed
  connecting from local machine to task's public IP on port 4004; paper account `DUQ351477`
  resolves through `managedAccounts()`.
- [DONE] Public-subnet placement chosen (no NAT Gateway).
- Fund the account within 45 days of approval (margin minimum $2,000) or it auto-closes.
- Pick the live options-data feed source — leaning IBKR feed via the Fargate Gateway (one
  source for signal + execution). Finnhub's option-chain endpoint has reported ATM-price/IV
  inaccuracy that would corrupt the implied-move signal; avoid for chains.
- [DONE 2026-05-23] OptionsChainFetcher → Gateway networking: resolved by the two-stack
  consolidation (§5). The Lambda will run in ComputeStack's VPC and reach Gateway over its
  VPC-internal address — no SG opening, no NAT-with-EIP needed.

---

_Not financial advice. Earnings options strategies frequently have negative expectancy after
spreads and commissions — especially the enter-before-announcement variant used here, which eats
the full IV crush. Recommendation-only mode against live data is the gate before risking real
money._
