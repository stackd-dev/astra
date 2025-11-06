import { SQSClient, SendMessageCommand } from "@aws-sdk/client-sqs";
import { FinlightApi } from "finlight-client";
import { NewsArticlePayload } from "./types.js";

const REGION = process.env.AWS_REGION ?? "us-east-1";
const QUEUE_URL = process.env.QUEUE_URL!;
const FINLIGHT_TOKEN = process.env.FEED_TOKEN!;
const LOG_LEVEL = process.env.LOG_LEVEL ?? "info";

if (!QUEUE_URL || !FINLIGHT_TOKEN) {
  console.error(
    "Missing required environment variables QUEUE_URL or FEED_TOKEN"
  );
  process.exit(1);
}

const sqs = new SQSClient({ region: REGION });

const logLevels = ["error", "warn", "info", "debug"] as const;
type LogLevel = (typeof logLevels)[number];

function log(level: LogLevel, ...args: unknown[]) {
  if (logLevels.indexOf(level) <= logLevels.indexOf(LOG_LEVEL as LogLevel)) {
    console.log(new Date().toISOString(), `[${level.toUpperCase()}]`, ...args);
  }
}

// ---------------- FINLIGHT IMPLEMENTATION ----------------

async function startFinlightListener(apiKey: string) {
  const client = new FinlightApi(
    { apiKey },
    { takeover: true } // auto close old connections if limit reached
  );

  log("info", "Connecting to Finlight WebSocket…");

  client.websocket.connect(
    {
      query: "(Nvidia OR OpenAI)",
      language: "en",
      countries: ["us"],
    },
    async (article: any) => {
      try {
        console.log(
          `Received article: ${article.title} at ${new Date().toISOString()}`
        );

        const payload: NewsArticlePayload = {
          provider: "finlight",
          headline: article.title,
          source: article.source ?? "unknown",
          url: article.url ?? "",
          publishedAt: article.publishedAt ?? new Date().toISOString(),
          summary: article.summary ?? "",
          companies: article.companies ?? [],
          raw: article,
        };

        await sqs.send(
          new SendMessageCommand({
            QueueUrl: QUEUE_URL,
            MessageBody: JSON.stringify(payload),
          })
        );

        log("info", "Pushed article to SQS:", payload.headline);
      } catch (err) {
        log("warn", "Failed to process article:", (err as Error).message);
      }
    }
  );
}

// ---------------- ENTRYPOINT ----------------

async function main() {
  await startFinlightListener(FINLIGHT_TOKEN);
  process.stdin.resume(); // keep container alive
}

main().catch((err) => {
  log("error", "Fatal error:", err);
  process.exit(1);
});
