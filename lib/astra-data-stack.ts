import { Stack, StackProps, RemovalPolicy } from "aws-cdk-lib";
import { Construct } from "constructs";
import { Queue } from "aws-cdk-lib/aws-sqs";
import { Topic } from "aws-cdk-lib/aws-sns";
import { Table, AttributeType, BillingMode } from "aws-cdk-lib/aws-dynamodb";
import { Secret } from "aws-cdk-lib/aws-secretsmanager";

export class AstraDataStack extends Stack {
  public readonly queue: Queue;
  public readonly alertsTopic: Topic;
  public readonly seenTable: Table;
  public readonly feedSecret: Secret;

  constructor(scope: Construct, id: string, props?: StackProps) {
    super(scope, id, props);

    this.queue = new Queue(this, "HeadlinesQueue");

    this.alertsTopic = new Topic(this, "AlertsTopic");

    this.seenTable = new Table(this, "SeenArticles", {
      partitionKey: { name: "contentHash", type: AttributeType.STRING },
      timeToLiveAttribute: "ttl",
      billingMode: BillingMode.PAY_PER_REQUEST,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    this.feedSecret = new Secret(this, "FeedToken");
  }
}
