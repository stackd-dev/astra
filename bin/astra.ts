#!/usr/bin/env node
import "source-map-support/register";
import { App } from "aws-cdk-lib";
import { AstraDataStack } from "../lib/astra-data-stack";
import { AstraIngestStack } from "../lib/astra-ingest-stack";
import { AstraProcessorStack } from "../lib/astra-processor-stack";

const app = new App();

// Base infrastructure
const data = new AstraDataStack(app, "AstraDataStack", {});

// Ingestion (Finnhub → SQS)
new AstraIngestStack(app, "AstraIngestStack", {
  queue: data.queue,
  finnhubSecret: data.finnhubSecret,
});

// Processing (filtering + Slack)
new AstraProcessorStack(app, "AstraProcessorStack", {
  queue: data.queue,
  alertsTopic: data.alertsTopic,
  seenTable: data.seenTable,
});
