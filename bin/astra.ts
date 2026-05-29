#!/usr/bin/env node
import { App } from "aws-cdk-lib";
import { CoreStack } from "../lib/core-stack";
import { ComputeStack } from "../lib/compute-stack";

const app = new App();

const AZ = "us-east-1a";

const core = new CoreStack(app, "Astra-CoreStack");

new ComputeStack(app, "Astra-ComputeStack", {
  earningsCalendarTable: core.earningsCalendarTable,
  historicalMovesTable: core.historicalMovesTable,
  sentimentScoresTable: core.sentimentScoresTable,
  pendingTradesTable: core.pendingTradesTable,
  livePositionsTable: core.livePositionsTable,
  tradeLogTable: core.tradeLogTable,
  seenContentTable: core.seenContentTable,
  newsFeedQueue: core.newsFeedQueue,
  alertsTopic: core.alertsTopic,
  ibkrCredentialsSecret: core.ibkrCredentialsSecret,
  dataFeedCredentialsSecret: core.dataFeedCredentialsSecret,
  redditCredentialsSecret: core.redditCredentialsSecret,
  alertsCredentialsSecret: core.alertsCredentialsSecret,
  configBucket: core.configBucket,
  availabilityZone: AZ,
});
