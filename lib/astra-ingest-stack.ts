import { Stack, StackProps, Duration } from "aws-cdk-lib";
import { Construct } from "constructs";
import { Queue } from "aws-cdk-lib/aws-sqs";
import { NodejsFunction } from "aws-cdk-lib/aws-lambda-nodejs";
import { Runtime } from "aws-cdk-lib/aws-lambda";
import { join } from "path";
import { Rule, Schedule } from "aws-cdk-lib/aws-events";
import { LambdaFunction } from "aws-cdk-lib/aws-events-targets";
import { Secret } from "aws-cdk-lib/aws-secretsmanager";

interface AstraIngestStackProps extends StackProps {
  queue: Queue;
  finnhubSecret: Secret;
}

export class AstraIngestStack extends Stack {
  constructor(scope: Construct, id: string, props: AstraIngestStackProps) {
    super(scope, id, props);

    const poller = new NodejsFunction(this, "FinnhubPoller", {
      runtime: Runtime.NODEJS_20_X,
      entry: join(__dirname, "../lambdas/ingest/poller.ts"),
      timeout: Duration.seconds(30),
      environment: {
        QUEUE_URL: props.queue.queueUrl,
        FINNHUB_SECRET_ARN: props.finnhubSecret.secretArn,
      },
    });

    props.queue.grantSendMessages(poller);
    props.finnhubSecret.grantRead(poller);

    // Run every minute
    new Rule(this, "PollSchedule", {
      schedule: Schedule.rate(Duration.minutes(1)),
      targets: [new LambdaFunction(poller)],
    });
  }
}
