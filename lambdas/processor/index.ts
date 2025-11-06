import { SQSEvent } from "aws-lambda";
import { SNSClient, PublishCommand } from "@aws-sdk/client-sns";
import { DynamoDBClient, PutItemCommand } from "@aws-sdk/client-dynamodb";
import { NewsArticlePayload } from "./types";

// --- AWS clients (reused across invocations)
const sns = new SNSClient({});
const db = new DynamoDBClient({});

// --- Environment variables
const ALERTS_TOPIC_ARN = process.env.ALERTS_TOPIC_ARN!;
const SEEN_TABLE = process.env.SEEN_TABLE!;

// --- NVIDIA-related keywords
const NVIDIA_KEYWORDS = [
  "NVIDIA",
  "NVDA",
  "CUDA",
  "TensorRT",
  "DGX",
  "NIM",
  "Blackwell",
  "Hopper",
  "Grace Hopper",
  "Grace CPU",
  "H100",
  "H200",
  "B100",
  "Omniverse",
  "NVIDIA AI Enterprise",
  "NVIDIA AI Platform",
  "NVIDIA Inference",
  "NVIDIA Cloud",
  "NVIDIA GPU",
  "NVIDIA AI",
];

// --- OpenAI-related keywords
const OPENAI_KEYWORDS = [
  "OpenAI",
  "GPT-4",
  "GPT-4o",
  "GPT-4 Turbo",
  "ChatGPT",
  "DALL-E",
  "Whisper",
  "Codex",
  "Sora",
  "OpenAI API",
  "OpenAI models",
  "OpenAI integration",
];

// --- Combined pattern for all key terms
const KEYWORDS = [...NVIDIA_KEYWORDS, ...OPENAI_KEYWORDS];
const pattern = new RegExp(`\\b(${KEYWORDS.join("|")})\\b`, "i");

export const handler = async (event: SQSEvent) => {
  await Promise.allSettled(event.Records.map(processRecord));
};

async function processRecord(record: SQSEvent["Records"][0]) {
  try {
    const msg: NewsArticlePayload = JSON.parse(record.body);
    const headline = msg.headline ?? "";
    const url = msg.url ?? "";
    const publishedAt = msg.publishedAt ?? new Date().toISOString();
    const id = Buffer.from(`${headline}-${publishedAt}`).toString("base64");

    // Filter: only process NVIDIA or OpenAI mentions
    if (!pattern.test(headline)) {
      console.log("⏭️ No relevant keywords found:", headline);
      return;
    }

    // Deduplicate using DynamoDB conditional put
    try {
      await db.send(
        new PutItemCommand({
          TableName: SEEN_TABLE,
          Item: {
            id: { S: id },
            headline: { S: headline },
            publishedAt: { S: publishedAt },
          },
          ConditionExpression: "attribute_not_exists(id)",
        })
      );
    } catch (err: any) {
      if (err.name === "ConditionalCheckFailedException") {
        console.log("⏭️ Duplicate news skipped:", headline);
        return;
      }
      throw err;
    }

    // Construct alert message
    const message = `🚀 *${headline}*\n${url}\nSource: ${
      msg.source ?? "Unknown"
    }\nPublished at: ${publishedAt}`;

    // Publish to SNS (this can be extended later to Slack, etc.)
    await sns.send(
      new PublishCommand({
        TopicArn: ALERTS_TOPIC_ARN,
        Subject: "News Alert",
        Message: message,
      })
    );

    console.log(
      JSON.stringify({
        level: "info",
        action: "alert_sent",
        headline,
        timestamp: new Date().toISOString(),
      })
    );
  } catch (err) {
    console.error("❌ Error processing record:", err);
  }
}
