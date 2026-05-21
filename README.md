# astra

A semi-automated earnings options trading system. The system researches
upcoming earnings, auto-enters options positions the afternoon before the
announcement, holds exactly one overnight, and surfaces the exit decision to a
human at the next market open. **Entry is automated; exit is manual.**

The repo started as a real-time news-alerter prototype and has been rewritten
ground-up into the trading system described here. Validated patterns from the
prior version (persistent WebSocket → SQS → processor, content-hash dedup,
Secrets-Manager-everything, modular stacks) are kept as guidelines, not code.

## Source of truth

Read these before changing anything in the repo:

- **[CLAUDE.md](CLAUDE.md)** — lean rules-and-decisions file (auto-loaded for
  Claude Code sessions). The trade pattern, stack layout, tech choices, safety
  rails, build sequence, and the list of explicitly-rejected ideas.
- **[docs/earnings-system-design.md](docs/earnings-system-design.md)** — full
  per-component reference: timing clock, per-component specs, data contracts,
  DynamoDB schemas, S3 paths, the two operating modes.

## The core trade pattern (one paragraph)

Enter the afternoon before the earnings announcement (~12:30 PT, the final ~30
minutes before the 1pm PT close), hold exactly one overnight, exit at the next
market open. AMC tickers (report after-close) give an after-hours preview
that's a signal only — retail can't trade options after-hours; the monitor
folds it into the morning exit alert. BMO tickers (report pre-open) get no
preview — react at the bell. Same `hold_until` rule for both. There is no
same-day intraday path; entry is always the prior afternoon. Edge is from the
implied vs. historical earnings move; positions are slightly-OTM weekly options
sized to lose 25–33% of per-trade capital if they go to zero (they regularly
will). See CLAUDE.md §2, §7.

## Stack layout

Three CDK stacks, partitioned by deployment lifecycle and blast radius (not
the pipeline diagram):

| Stack | Status | Role |
|---|---|---|
| **Astra-CoreStack** ([lib/core-stack.ts](lib/core-stack.ts)) | scaffolded | Stateful, long-lived: DynamoDB tables, SQS + DLQ, SNS alerts topic, Secrets Manager, S3 config bucket (STOP file + `strategy.yaml`), EBS volume for the IB Gateway box. |
| **Astra-PipelineStack** | not yet built | Stateless research-and-decision brain: earnings/chains/news/Reddit fetchers, Bedrock sentiment, strategy/scoring engine. No money at risk; iterate freely. |
| **Astra-ExecutionStack** | not yet built | Money-at-risk: IB Gateway EC2 box, `OrderExecutor`, `PositionMonitor`. Isolated deliberately; built last, deployed most carefully. |

Why exactly three: see CLAUDE.md §5.

## Build sequence

Validation is forward — there is no backtester. Build the real system, run it
in **recommendation-only mode** against live data, review the calls against
actual market opens, then flip to live money once the owner confirms.

1. Build the real infrastructure: Astra-CoreStack → Astra-PipelineStack → Astra-ExecutionStack.
2. Run in recommendation-only mode (`execution_mode=recommend` on the
   OrderExecutor) — full pipeline, no orders submitted, recommendations logged.
3. Daily review loop — owner judges recommended trades against actual opens.
4. Flip to live, tiny — 1 contract; scale only after 20+ clean live trades.

Full sequence in CLAUDE.md §12 and [docs/earnings-system-design.md §5](docs/earnings-system-design.md#5-build-sequence-validation-via-recommendation-only-mode--no-backtester).

## Working with the CDK app

```bash
npm install          # install CDK + TS toolchain
npm run build        # tsc (type-check + emit to dist/)
npx cdk ls           # list stacks (currently just Astra-CoreStack)
npm run synth        # cdk synth — render CloudFormation
npm run diff         # cdk diff — diff against deployed state
npm run deploy       # build + synth + deploy
```

The EBS volume in Astra-CoreStack is pinned to `us-east-1a` (hardcoded in
[bin/astra.ts](bin/astra.ts)). Astra-ExecutionStack must launch its EC2
instance in that same AZ to attach the volume.

Infra is TypeScript (CDK); the data/quant logic that runs inside the stacks
(Lambdas, Fargate tasks, the EC2 service) will be Python. See CLAUDE.md §4.

## Layout

```
bin/astra.ts                     CDK app entry (cdk.json points here)
lib/core-stack.ts                CoreStack — stateful infra
docs/earnings-system-design.md   detailed design reference
CLAUDE.md                        rules + decisions (auto-loaded)
```

## Owner to-dos (external lead time — start now)

- Open IBKR account, apply for options Level 3, enable API access (2–3 week lead).
- Pick the live options-data feed (IBKR feed / Finnhub / Polygon) — feeds the
  nightly OptionsChainFetcher.
- Decide NAT Gateway vs. public-subnet placement for the IB Gateway EC2 box.

See CLAUDE.md §14.

---

*Not financial advice. Earnings options strategies frequently have negative
expectancy after spreads and commissions — especially the enter-before-
announcement variant used here, which eats the full IV crush. Recommendation-
only mode against live data is the gate before risking real money.*
