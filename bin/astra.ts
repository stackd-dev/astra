#!/usr/bin/env node
import { App } from "aws-cdk-lib";
import { CoreStack } from "../lib/core-stack";
import { ComputeStack } from "../lib/compute-stack";

const app = new App();

const AZ = "us-east-1a";

const core = new CoreStack(app, "Astra-CoreStack");

new ComputeStack(app, "Astra-ComputeStack", {
  earningsCalendarTable: core.earningsCalendarTable,
  dataFeedCredentialsSecret: core.dataFeedCredentialsSecret,
  ibkrCredentialsSecret: core.ibkrCredentialsSecret,
  availabilityZone: AZ,
});
