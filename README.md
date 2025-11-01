# 🌌 Astra

**Real-time Market Signal Detection System**

---

## Overview

Astra continuously monitors real-time financial news to detect market-moving announcements — such as partnerships or collaborations involving NVIDIA or OpenAI.
When a relevant headline appears, Astra filters it, deduplicates it, and sends an instant alert to Slack — typically within 2–5 seconds of publication.

Astra is the signal intelligence layer that can later power automated trading, sentiment analysis, or research workflows.

---

## System Architecture

Astra is composed of three modular AWS CDK stacks:

[Finnhub / Polygon / Benzinga API]
  ↓
AstraIngestStack → Fetch headlines (polling or WebSocket)
AstraDataStack → Core shared infrastructure
AstraProcessorStack → Filtering + dedup + notifications
  ↓
Slack / SMS / Email

---

## Stack Breakdown

### AstraDataStack — Core Infrastructure

| Resource                             | Purpose                                                      |
| ------------------------------------ | ------------------------------------------------------------ |
| SQS Queue – HeadlinesQueue           | Buffers incoming headlines from the ingestion service        |
| SNS Topic – AlertsTopic              | Broadcasts filtered alerts to subscribers (Slack, SMS, etc.) |
| DynamoDB Table – SeenArticles        | Tracks processed headlines to prevent duplicates (TTL 48h)   |
| Secrets – FinnhubToken, SlackWebhook | Stores credentials for data feed and Slack alerts            |

This stack is long-lived and rarely redeployed. All other stacks depend on it.

---

### AstraIngestStack — Data Ingestion

| Component            | Description                                                    |
| -------------------- | -------------------------------------------------------------- |
| FinnhubPoller Lambda | Polls Finnhub’s REST API every minute for the latest headlines |
| EventBridge Rule     | Triggers the poller Lambda every 1 minute                      |
| IAM Grants           | Grants permission to read secrets and send messages to SQS     |

Future upgrade: replace the poller with a persistent Fargate WebSocket listener for sub-second latency.

---

### AstraProcessorStack — Signal Detection

| Component                | Description                                                                                                         |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------- |
| HeadlineProcessor Lambda | Consumes headlines from SQS, applies keyword/NLP filters, deduplicates using DynamoDB, and publishes results to SNS |
| Notifier Lambda (future) | Subscribes to SNS and posts alerts to Slack or other channels                                                       |
| IAM Grants               | Access to DynamoDB, SNS, Secrets Manager, and SQS                                                                   |

Logic highlights:

- Detects phrases like “announces partnership with NVIDIA” or “enters into agreement with OpenAI”.
- Ignores noise such as “compatible with”, “developer program”, or “uses NVIDIA”.
- Uses a SHA-256 hash of headline + source + time to prevent duplicates.

---

## Example Alert

⚡ ACME Robotics — partnership with NVIDIA
BusinessWire • 09:14 ET
[Read article →](https://example.com/pr/1234)

---

## Deployment Model

Each stack is a CDK construct and can be deployed independently or all at once.

one-time setup:
cdk bootstrap

deploy everything:
cdk deploy --all

or individually:
cdk deploy AstraDataStack
cdk deploy AstraIngestStack
cdk deploy AstraProcessorStack

Dependencies:

- AstraIngestStack → depends on AstraDataStack
- AstraProcessorStack → depends on AstraDataStack

---

## Local Setup

1. Install dependencies
   npm install

2. Add secrets in AWS Secrets Manager

   - FinnhubToken → "YOUR_FINNHUB_API_KEY"
   - SlackWebhook → {"url":"[https://hooks.slack.com/services/..."}](https://hooks.slack.com/services/...%22})

3. Deploy
   cdk deploy --all

4. Check Slack
   You’ll start receiving alerts whenever a matching headline is detected.

---

## Estimated Monthly Cost

| Component       | Cost            | Notes                |
| --------------- | --------------- | -------------------- |
| AWS Lambda      | $1–3            | Poller + Processor   |
| EventBridge     | <$1             | 1-minute schedule    |
| DynamoDB        | $3–5            | Pay-per-request mode |
| SQS + SNS       | <$1             | Light traffic        |
| Secrets Manager | $2              | Two secrets          |
| CloudWatch Logs | $2              | Basic logging        |
| AWS subtotal    | ≈ $10–15 / mo   |                      |
| Finnhub Pro API | $100 / mo       | Data source          |
| Total           | ≈ $110–120 / mo | End-to-end operation |

---

## Design Principles

- Serverless-first — fully managed, minimal ops
- Modular stacks — independent deployment cycles
- Low-latency pipeline — <5 s (polling) or <1 s (WebSocket)
- Extensible — add new feeds, ML models, or broker integrations
- Cost-efficient — runs under free/low tiers for AWS

---

## Roadmap

| Phase | Goal                                    | Implementation                                |
| ----- | --------------------------------------- | --------------------------------------------- |
| 1     | MVP with Finnhub polling + Slack alerts | Lambda poller + regex filter                  |
| 2     | Real-time WebSocket ingestion           | Fargate streaming listener                    |
| 3     | Entity extraction & sentiment scoring   | Amazon Comprehend / Bedrock                   |
| 4     | Automated trading signals               | Add AstraTradingStack                         |
| 5     | Analytics dashboards                    | Add AstraAnalyticsStack (Athena + QuickSight) |

---

## Repository Structure

astra/
├── bin/
│ └── astra.ts — CDK app entrypoint
├── lib/
│ ├── astra-data-stack.ts — Core infrastructure
│ ├── astra-ingest-stack.ts — Feed ingestion
│ └── astra-processor-stack.ts — Filtering + alerts
├── lambdas/
│ ├── ingest/ — Finnhub poller or Fargate listener
│ ├── processor/ — Headline processor logic
│ └── notifier/ — Slack notifier (future)
├── package.json
├── tsconfig.json
└── README.md

---

## Tech Stack

| Layer      | Technology                    |
| ---------- | ----------------------------- |
| IaC        | AWS CDK (TypeScript)          |
| Compute    | AWS Lambda / Fargate (future) |
| Messaging  | Amazon SQS + SNS              |
| Storage    | Amazon DynamoDB               |
| Secrets    | AWS Secrets Manager           |
| Scheduling | Amazon EventBridge            |
| Monitoring | Amazon CloudWatch             |
| Language   | TypeScript (Node 20 runtime)  |

---

## Naming Lineage

| Project   | Domain                             | Theme                            |
| --------- | ---------------------------------- | -------------------------------- |
| Northstar | Equirig (marketplace intelligence) | Direction / guidance             |
| Nova      | Tithi (matchmaking intelligence)   | New light / connection           |
| Astra     | Market signal intelligence         | Celestial watcher / alert system |

---

## Philosophy

“Astra is designed to see before others.”

Astra doesn’t just collect data — it listens for meaningful signals, filters noise, and acts instantly.
This foundation will evolve into an autonomous market-intelligence and trading-signal system.

---

## Useful commands

- `npm run build` compile typescript to js
- `npm run watch` watch for changes and compile
- `npm run test` perform the jest unit tests
- `npx cdk deploy` deploy this stack to your default AWS account/region
- `npx cdk diff` compare deployed stack with current state
- `npx cdk synth` emits the synthesized CloudFormation template
