#!/usr/bin/env node
import { App } from "aws-cdk-lib";
import { CoreStack } from "../lib/core-stack";
import { PipelineStack } from "../lib/pipeline-stack";
import { ExecutionStack } from "../lib/execution-stack";

const app = new App();

const AZ = "us-east-1a";

const core = new CoreStack(app, "Astra-CoreStack");

new PipelineStack(app, "Astra-PipelineStack", {
  earningsCalendarTable: core.earningsCalendarTable,
  dataFeedCredentialsSecret: core.dataFeedCredentialsSecret,
});

new ExecutionStack(app, "Astra-ExecutionStack", {
  ibkrCredentialsSecret: core.ibkrCredentialsSecret,
  availabilityZone: AZ,
});
