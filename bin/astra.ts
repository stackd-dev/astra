#!/usr/bin/env node
import { App } from "aws-cdk-lib";
import { CoreStack } from "../lib/core-stack";

const app = new App();

new CoreStack(app, "Astra-CoreStack", { availabilityZone: "us-east-1a" });
