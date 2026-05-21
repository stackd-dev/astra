#!/usr/bin/env node
import { App } from "aws-cdk-lib";
import { CoreStack } from "../lib/core-stack";
import { PipelineStack } from "../lib/pipeline-stack";

const app = new App();

const core = new CoreStack(app, "Astra-CoreStack", {
  availabilityZone: "us-east-1a",
});

new PipelineStack(app, "Astra-PipelineStack", {
  earningsCalendarTable: core.earningsCalendarTable,
  dataFeedCredentialsSecret: core.dataFeedCredentialsSecret,
});
