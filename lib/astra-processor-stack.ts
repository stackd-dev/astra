import { Duration, Stack, StackProps } from "aws-cdk-lib";
import { Construct } from "constructs";
import { Queue } from "aws-cdk-lib/aws-sqs";
import { Table } from "aws-cdk-lib/aws-dynamodb";
import { Topic } from "aws-cdk-lib/aws-sns";
import { NodejsFunction } from "aws-cdk-lib/aws-lambda-nodejs";
import { SqsEventSource } from "aws-cdk-lib/aws-lambda-event-sources";
import { Runtime } from "aws-cdk-lib/aws-lambda";
import { join } from "path";

interface AstraProcessorStackProps extends StackProps {
  queue: Queue;
  alertsTopic: Topic;
  seenTable: Table;
}

export class AstraProcessorStack extends Stack {
  constructor(scope: Construct, id: string, props: AstraProcessorStackProps) {
    super(scope, id, props);

    const processor = new NodejsFunction(this, "HeadlineProcessor", {
      runtime: Runtime.NODEJS_20_X,
      entry: join(__dirname, "../lambdas/processor/index.ts"),
      timeout: Duration.seconds(15),
      environment: {
        ALERTS_TOPIC_ARN: props.alertsTopic.topicArn,
        SEEN_TABLE: props.seenTable.tableName,
      },
    });
    processor.addEventSource(new SqsEventSource(props.queue));

    props.queue.grantConsumeMessages(processor);
    props.seenTable.grantReadWriteData(processor);
    props.alertsTopic.grantPublish(processor);
  }
}
