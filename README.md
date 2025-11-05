# 🌌 Astra

**Real-Time Market Signal Detection System**

---

## 🧭 Overview

**Astra** continuously monitors financial news feeds to detect **market-moving announcements** — for example, partnerships or collaborations involving **NVIDIA** or **OpenAI**.
When a relevant headline appears, Astra filters it, deduplicates it, and sends an instant alert to Slack — typically within **one second** of publication.

Astra forms the **signal intelligence layer** that can later power automated trading, sentiment analysis, or research workflows.

---

## 🛰️ System Architecture

Astra is built as a modular, fully serverless AWS system composed of **three independent CDK stacks**:

```
[Finnhub WebSocket Stream]
        │
        ▼
 ┌─────────────────────┐
 │ AstraIngestStack    │ → Real-time Fargate WebSocket listener
 │  • ECS Fargate task │
 │  • Pushes to SQS    │
 └─────────────────────┘
        │
        ▼
 ┌─────────────────────┐
 │ AstraDataStack      │ → Core shared infrastructure
 │  • SQS Queue        │
 │  • SNS Topic        │
 │  • DynamoDB Table   │
 │  • Secrets          │
 └─────────────────────┘
        │
        ▼
 ┌─────────────────────┐
 │ AstraProcessorStack │ → Filtering + dedup + Slack alerts
 │  • HeadlineProcessor│
 │  • (future) Notifier│
 └─────────────────────┘
        │
        ▼
   [Slack / Email / SMS]
```

---

## ⚙️ Stack Breakdown

### 🧱 **1️⃣ AstraDataStack — Core Infrastructure**

| Resource                                     | Description                                         |
| -------------------------------------------- | --------------------------------------------------- |
| **SQS Queue – `HeadlinesQueue`**             | Buffers incoming headlines.                         |
| **SNS Topic – `AlertsTopic`**                | Broadcasts filtered alerts to multiple subscribers. |
| **DynamoDB Table – `SeenArticles`**          | Deduplication using content hash (TTL 48 h).        |
| **Secrets – `FinnhubToken`, `SlackWebhook`** | Stores API keys and alert webhooks securely.        |

➡️ Long-lived, foundational stack shared across all others.

---

### 📡 **2️⃣ AstraIngestStack — Real-Time Data Ingestion (Fargate WebSocket)**

| Component                           | Description                                                                                                           |
| ----------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| **FeedListener (ECS Fargate task)** | Maintains a persistent WebSocket connection to **Finnhub** and streams headlines to SQS in real time (< 1 s latency). |
| **Container image**                 | Built from `/services/feed-listener` during CDK deployment.                                                           |
| **Auto-Reconnect**                  | Reconnects automatically on network drop.                                                                             |
| **Networking**                      | Public subnet, outbound HTTPS (no NAT needed).                                                                        |
| **Logging**                         | CloudWatch Logs group `astra-feed`.                                                                                   |

**Environment/Secrets**

- `PROVIDER=finnhub`
- `QUEUE_URL` – set by CDK
- `FEED_TOKEN_SECRET_ARN` – ARN of `FinnhubToken` secret
- `LOG_LEVEL=info` (optional)

**Typical cost:** ~$9–10/month for continuous operation.

**Fallback option:**
A polling Lambda via EventBridge (~$1/month, 30 s–1 min latency) can be used for testing, but WebSocket is recommended for production.

---

### 🧮 **3️⃣ AstraProcessorStack — Signal Detection & Alerts**

| Component                    | Description                                                                                                                 |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| **HeadlineProcessor Lambda** | Consumes messages from SQS, filters for NVIDIA/OpenAI partnerships, deduplicates via DynamoDB, and publishes alerts to SNS. |
| **Notifier Lambda (future)** | Subscribes to SNS and posts alerts to Slack.                                                                                |
| **IAM Grants**               | Access to SQS, DynamoDB, SNS, and Secrets Manager.                                                                          |

**Filtering logic**

- Positive phrases: “announces partnership with”, “collaboration with NVIDIA”, “deal with OpenAI”.
- Negative filters: “compatible with”, “developer program”, “uses NVIDIA chip”.
- SHA-256 hash ensures duplicates aren’t re-alerted.

---

## 💬 Example Alert

> ⚡ **ACME Robotics** — partnership with **NVIDIA** > _BusinessWire • 09:14 ET_ > [Read article →](https://example.com/pr/1234)

---

## 🧩 Deployment

1. **Bootstrap once per region/account**

   ```
   cdk bootstrap
   ```

2. **Deploy all stacks**

   ```
   cdk deploy --all
   ```

   or individually:

   ```
   cdk deploy AstraDataStack
   cdk deploy AstraIngestStack
   cdk deploy AstraProcessorStack
   ```

3. **Add secrets manually (AWS Console → Secrets Manager)**

   - `FinnhubToken`: `{"token":"YOUR_FINNHUB_API_KEY"}`
   - `SlackWebhook`: `{"url":"https://hooks.slack.com/services/..."}`

4. **Validate deployment**

   - Check CloudWatch Logs → `astra-feed` → look for “Connected to Finnhub”.
   - New headlines appear in SQS and trigger the processor.

---

## 💰 Monthly Cost Estimate

| Component           | Est. Cost         | Notes                        |
| ------------------- | ----------------- | ---------------------------- |
| ECS Fargate Task    | $9–10             | Always-on 0.25 vCPU / 0.5 GB |
| SQS + SNS           | <$1               | Low message volume           |
| DynamoDB            | $3–5              | Pay-per-request mode         |
| Secrets Manager     | $2                | Two secrets                  |
| CloudWatch Logs     | ~$2               | Moderate logging             |
| **AWS Subtotal**    | **≈ $15–20/mo**   |                              |
| **Finnhub Pro API** | **$100/mo**       | News feed                    |
| **Total**           | **≈ $115–120/mo** | End-to-end operation         |

---

## 🧠 Design Principles

- **Always-on real-time ingestion** – < 1 s latency from newswire to alert.
- **Serverless core** – minimal operational overhead.
- **Modular CDK stacks** – clean dependency boundaries.
- **Scalable by design** – supports more feeds or symbols easily.
- **Cost-efficient** – predictable monthly compute cost.

---

## 🚀 Roadmap

| Phase | Goal                                      | Implementation                              |
| ----- | ----------------------------------------- | ------------------------------------------- |
| **1** | MVP with Finnhub WebSocket + Slack alerts | Fargate listener + regex filter             |
| **2** | Add more data providers                   | Additional WebSocket containers             |
| **3** | Sentiment & entity analysis               | Amazon Comprehend / Bedrock                 |
| **4** | Trading signal generation                 | Add `AstraTradingStack`                     |
| **5** | Analytics & dashboards                    | `AstraAnalyticsStack` (Athena + QuickSight) |

---

## 🪜 Repository Structure

```
astra/
├── bin/
│   └── astra.ts                 # CDK app entrypoint
├── lib/
│   ├── astra-data-stack.ts      # Core infra (SQS, SNS, DynamoDB, Secrets)
│   ├── astra-ingest-stack.ts    # Fargate WebSocket ingestion
│   └── astra-processor-stack.ts # Filtering + alerts
├── services/
│   └── feed-listener/           # Fargate WebSocket container
│       ├── Dockerfile
│       ├── package.json
│       └── src/index.ts
├── lambdas/
│   ├── processor/               # Headline processor Lambda
│   └── notifier/                # Slack notifier (future)
├── package.json
├── tsconfig.json
└── README.md
```

---

## 🧩 Tech Stack

| Layer          | Technology                   |
| -------------- | ---------------------------- |
| **IaC**        | AWS CDK (TypeScript)         |
| **Compute**    | AWS Lambda, ECS Fargate      |
| **Messaging**  | Amazon SQS, SNS              |
| **Storage**    | Amazon DynamoDB              |
| **Secrets**    | AWS Secrets Manager          |
| **Networking** | AWS VPC (public subnets)     |
| **Monitoring** | Amazon CloudWatch            |
| **Language**   | TypeScript (Node 20 runtime) |

---

## 🧭 Naming Lineage

| Project       | Domain                             | Theme                            |
| ------------- | ---------------------------------- | -------------------------------- |
| **Northstar** | Equirig (Marketplace Intelligence) | Direction / Guidance             |
| **Nova**      | Tithi (Matchmaking Intelligence)   | New Light / Connection           |
| **Astra**     | Market Signal Intelligence         | Celestial Watcher / Alert System |

---

## 🧩 Philosophy

> “**Astra is designed to see before others.**”

Astra doesn’t just collect data — it **listens** for meaningful signals, **filters** noise, and **acts instantly**.
It’s the foundation for an autonomous, intelligent market-monitoring and trading-signal ecosystem.

---

## Useful commands

- `npm run build` compile typescript to js
- `npm run watch` watch for changes and compile
- `npm run test` perform the jest unit tests
- `npx cdk deploy` deploy this stack to your default AWS account/region
- `npx cdk diff` compare deployed stack with current state
- `npx cdk synth` emits the synthesized CloudFormation template
