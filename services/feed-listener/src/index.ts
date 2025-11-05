import { SQSClient, SendMessageCommand } from "@aws-sdk/client-sqs";
import WebSocket from "ws";

const REGION = process.env.AWS_REGION ?? "us-east-1";
const QUEUE_URL = process.env.QUEUE_URL!;
const FINNHUB_TOKEN = process.env.FEED_TOKEN!; // ECS injects actual token value
const LOG_LEVEL = process.env.LOG_LEVEL ?? "info";

if (!QUEUE_URL || !FINNHUB_TOKEN) {
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

async function startFinnhubListener(token: string) {
  const url = `wss://ws.finnhub.io?token=${encodeURIComponent(token)}`;

  const connect = () => {
    log("info", "Connecting to Finnhub WebSocket…");
    const ws = new WebSocket(url);

    ws.on("open", () => log("info", "Connected to Finnhub WebSocket"));

    ws.on("message", async (raw) => {
      try {
        const msg = JSON.parse(raw.toString());
        if (!Array.isArray(msg.data)) return;
        for (const n of msg.data) {
          const payload = {
            provider: "finnhub",
            headline: n.headline,
            source: n.source,
            url: n.url,
            publishedAt: new Date(n.datetime || Date.now()).toISOString(),
            related: n.related ?? "",
            summary: n.summary ?? "",
          };
          await sqs.send(
            new SendMessageCommand({
              QueueUrl: QUEUE_URL,
              MessageBody: JSON.stringify(payload),
            })
          );
        }
      } catch (err) {
        log("warn", "Failed to parse message:", (err as Error).message);
      }
    });

    ws.on("close", () => {
      log("warn", "WebSocket closed. Reconnecting in 2s…");
      setTimeout(connect, 2000);
    });

    ws.on("error", (err) => {
      log("warn", "WebSocket error:", (err as Error).message);
      ws.close();
    });

    // keep-alive ping
    const heartbeat = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) ws.ping();
    }, 15000);

    ws.on("close", () => clearInterval(heartbeat));
  };

  connect();
}

async function main() {
  await startFinnhubListener(FINNHUB_TOKEN);
  process.stdin.resume(); // keep the container alive
}

main().catch((err) => {
  log("error", "Fatal error:", err);
  process.exit(1);
});
