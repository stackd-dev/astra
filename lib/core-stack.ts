import { Stack, StackProps, RemovalPolicy, Duration, Size, Tags, CfnOutput } from "aws-cdk-lib";
import { Construct } from "constructs";
import { Table, AttributeType, BillingMode } from "aws-cdk-lib/aws-dynamodb";
import { Queue } from "aws-cdk-lib/aws-sqs";
import { Topic } from "aws-cdk-lib/aws-sns";
import { Secret } from "aws-cdk-lib/aws-secretsmanager";
import { Bucket, BucketEncryption, BlockPublicAccess } from "aws-cdk-lib/aws-s3";
import { Volume, EbsDeviceVolumeType } from "aws-cdk-lib/aws-ec2";

export interface CoreStackProps extends StackProps {
  /** AZ for the IB Gateway EBS volume. ExecutionStack must launch its EC2 box in this same AZ. */
  readonly availabilityZone: string;
}

/** Stateful, long-lived infra for the earnings options trading system. See CLAUDE.md §5. */
export class CoreStack extends Stack {
  public readonly earningsCalendarTable: Table;
  public readonly historicalMovesTable: Table;
  public readonly sentimentScoresTable: Table;
  public readonly pendingTradesTable: Table;
  public readonly livePositionsTable: Table;
  public readonly tradeLogTable: Table;
  public readonly seenContentTable: Table;

  public readonly newsFeedQueue: Queue;
  public readonly newsFeedDeadLetterQueue: Queue;
  public readonly alertsTopic: Topic;

  public readonly ibkrCredentialsSecret: Secret;
  public readonly dataFeedCredentialsSecret: Secret;
  public readonly redditCredentialsSecret: Secret;
  public readonly alertsCredentialsSecret: Secret;

  public readonly configBucket: Bucket;
  public readonly ibGatewayVolume: Volume;

  constructor(scope: Construct, id: string, props: CoreStackProps) {
    super(scope, id, props);

    const RETAIN = RemovalPolicy.RETAIN;

    this.earningsCalendarTable = new Table(this, "EarningsCalendarTable", {
      partitionKey: { name: "date", type: AttributeType.STRING },
      sortKey: { name: "ticker", type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      removalPolicy: RETAIN,
    });

    this.historicalMovesTable = new Table(this, "HistoricalMovesTable", {
      partitionKey: { name: "ticker", type: AttributeType.STRING },
      sortKey: { name: "quarter", type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      removalPolicy: RETAIN,
    });

    this.sentimentScoresTable = new Table(this, "SentimentScoresTable", {
      partitionKey: { name: "ticker", type: AttributeType.STRING },
      sortKey: { name: "date", type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      removalPolicy: RETAIN,
    });

    this.pendingTradesTable = new Table(this, "PendingTradesTable", {
      partitionKey: { name: "trade_id", type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      removalPolicy: RETAIN,
    });

    this.livePositionsTable = new Table(this, "LivePositionsTable", {
      partitionKey: { name: "position_id", type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      removalPolicy: RETAIN,
    });

    this.tradeLogTable = new Table(this, "TradeLogTable", {
      partitionKey: { name: "trade_id", type: AttributeType.STRING },
      sortKey: { name: "ts", type: AttributeType.NUMBER },
      billingMode: BillingMode.PAY_PER_REQUEST,
      removalPolicy: RETAIN,
    });

    this.seenContentTable = new Table(this, "SeenContentTable", {
      partitionKey: { name: "content_hash", type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: "ttl",
      removalPolicy: RETAIN,
    });

    this.newsFeedDeadLetterQueue = new Queue(this, "NewsFeedDeadLetterQueue", {
      retentionPeriod: Duration.days(14),
      removalPolicy: RETAIN,
    });

    this.newsFeedQueue = new Queue(this, "NewsFeedQueue", {
      visibilityTimeout: Duration.seconds(60),
      retentionPeriod: Duration.days(4),
      deadLetterQueue: { queue: this.newsFeedDeadLetterQueue, maxReceiveCount: 5 },
      removalPolicy: RETAIN,
    });

    this.alertsTopic = new Topic(this, "AlertsTopic", { displayName: "Astra Alerts" });
    this.alertsTopic.applyRemovalPolicy(RETAIN);

    // Stable secret names so humans/consumers find them predictably; values populated post-deploy.
    this.ibkrCredentialsSecret = new Secret(this, "IbkrCredentialsSecret", {
      secretName: "astra/ibkr-credentials",
      description: "IBKR creds for IB Gateway auto-login (IBC). Fields: username, password, account_id, trading_mode (paper|live).",
      removalPolicy: RETAIN,
    });

    this.dataFeedCredentialsSecret = new Secret(this, "DataFeedCredentialsSecret", {
      secretName: "astra/data-feed-credentials",
      description: "Earnings/chain/news feed API keys. Fields: finnhub_api_key, polygon_api_key (whichever you pick — CLAUDE.md §11).",
      removalPolicy: RETAIN,
    });

    this.redditCredentialsSecret = new Secret(this, "RedditCredentialsSecret", {
      secretName: "astra/reddit-credentials",
      description: "Reddit (PRAW) OAuth. Fields: client_id, client_secret, username, password, user_agent.",
      removalPolicy: RETAIN,
    });

    this.alertsCredentialsSecret = new Secret(this, "AlertsCredentialsSecret", {
      secretName: "astra/alerts-credentials",
      description: "SMS provider creds. Fields (Twilio): account_sid, auth_token, from_number, to_number.",
      removalPolicy: RETAIN,
    });

    // Versioned so strategy.yaml weight tweaks across the recommendation-only review loop keep history.
    this.configBucket = new Bucket(this, "ConfigBucket", {
      versioned: true,
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      encryption: BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: RETAIN,
    });

    // EBS lives in CoreStack (not ExecutionStack) so the IB Gateway session survives any
    // ExecutionStack teardown/redeploy. ExecutionStack's EC2 instance must be in `availabilityZone`.
    this.ibGatewayVolume = new Volume(this, "IbGatewayVolume", {
      availabilityZone: props.availabilityZone,
      size: Size.gibibytes(8),
      volumeType: EbsDeviceVolumeType.GP3,
      encrypted: true,
      removalPolicy: RETAIN,
    });
    Tags.of(this.ibGatewayVolume).add("Purpose", "ib-gateway-session");

    new CfnOutput(this, "EarningsCalendarTableName", { value: this.earningsCalendarTable.tableName });
    new CfnOutput(this, "HistoricalMovesTableName", { value: this.historicalMovesTable.tableName });
    new CfnOutput(this, "SentimentScoresTableName", { value: this.sentimentScoresTable.tableName });
    new CfnOutput(this, "PendingTradesTableName", { value: this.pendingTradesTable.tableName });
    new CfnOutput(this, "LivePositionsTableName", { value: this.livePositionsTable.tableName });
    new CfnOutput(this, "TradeLogTableName", { value: this.tradeLogTable.tableName });
    new CfnOutput(this, "SeenContentTableName", { value: this.seenContentTable.tableName });
    new CfnOutput(this, "NewsFeedQueueUrl", { value: this.newsFeedQueue.queueUrl });
    new CfnOutput(this, "NewsFeedDeadLetterQueueUrl", { value: this.newsFeedDeadLetterQueue.queueUrl });
    new CfnOutput(this, "AlertsTopicArn", { value: this.alertsTopic.topicArn });
    new CfnOutput(this, "ConfigBucketName", { value: this.configBucket.bucketName });
    new CfnOutput(this, "IbGatewayVolumeId", { value: this.ibGatewayVolume.volumeId });
    new CfnOutput(this, "IbGatewayVolumeAz", { value: props.availabilityZone });
  }
}
