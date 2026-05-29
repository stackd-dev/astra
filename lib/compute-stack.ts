import * as path from "path";
import { Stack, StackProps, RemovalPolicy, Duration, CfnOutput } from "aws-cdk-lib";
import { Construct } from "constructs";
import {
  Vpc,
  SubnetType,
  SecurityGroup,
  Port,
  IVpc,
  GatewayVpcEndpointAwsService,
  InterfaceVpcEndpointAwsService,
} from "aws-cdk-lib/aws-ec2";
import {
  Cluster,
  FargateTaskDefinition,
  FargateService,
  ContainerImage,
  LogDrivers,
  Secret as EcsSecret,
  FargatePlatformVersion,
  CpuArchitecture,
  OperatingSystemFamily,
} from "aws-cdk-lib/aws-ecs";
import {
  FileSystem,
  PerformanceMode,
  ThroughputMode,
  AccessPoint,
} from "aws-cdk-lib/aws-efs";
import { Function, Runtime, Code, Architecture } from "aws-cdk-lib/aws-lambda";
import { Rule, Schedule } from "aws-cdk-lib/aws-events";
import { SfnStateMachine } from "aws-cdk-lib/aws-events-targets";
import { Table } from "aws-cdk-lib/aws-dynamodb";
import { Secret } from "aws-cdk-lib/aws-secretsmanager";
import { Bucket } from "aws-cdk-lib/aws-s3";
import { Topic } from "aws-cdk-lib/aws-sns";
import { Queue } from "aws-cdk-lib/aws-sqs";
import { LogGroup, RetentionDays } from "aws-cdk-lib/aws-logs";
import { PolicyStatement, Effect } from "aws-cdk-lib/aws-iam";
import { PrivateDnsNamespace, DnsRecordType } from "aws-cdk-lib/aws-servicediscovery";
import {
  StateMachine,
  Choice,
  Condition,
  Pass,
  Succeed,
  Parallel,
  Map as SfnMap,
  StateMachineType,
  LogLevel,
  TaskInput,
  DefinitionBody,
} from "aws-cdk-lib/aws-stepfunctions";
import { LambdaInvoke } from "aws-cdk-lib/aws-stepfunctions-tasks";

export interface ComputeStackProps extends StackProps {
  readonly earningsCalendarTable: Table;
  readonly historicalMovesTable: Table;
  readonly sentimentScoresTable: Table;
  readonly pendingTradesTable: Table;
  readonly livePositionsTable: Table;
  readonly tradeLogTable: Table;
  readonly seenContentTable: Table;

  readonly newsFeedQueue: Queue;
  readonly alertsTopic: Topic;

  readonly ibkrCredentialsSecret: Secret;
  readonly dataFeedCredentialsSecret: Secret;
  readonly redditCredentialsSecret: Secret;
  readonly alertsCredentialsSecret: Secret;

  readonly configBucket: Bucket;
  readonly availabilityZone: string;
}

/**
 * ComputeStack — everything stateless. VPC + ECS cluster shared by all
 * Fargate tasks and VPC-attached Lambdas. Two Step Functions, two
 * EventBridge schedules, two always-on Fargate services (IB Gateway, news
 * ingestion, position monitor). See CLAUDE.md §5 + design doc.
 *
 * Pipelines:
 *   Research SFN @ 06:00 UTC (≈ 01:00 ET):
 *     EarningsFetcher
 *       └─ Map(candidates) → HistoricalMovesFetcher
 *
 *   DecideAndExecute SFN @ 19:00 UTC (≈ 15:00 ET):
 *     Parallel:
 *       ├─ Map(candidates) → OptionsChainFetcher    (fresh chain right now)
 *       └─ SentimentProcessor                        (fresh Bedrock pass)
 *     ↓
 *     StrategyEngine
 *     ↓
 *     PreFlightChecks (STOP file, caps, mode, time gate)
 *     ↓
 *     Choice on execution_mode:
 *       recommend → RecordTrades → SendRecapSMS
 *       live      → Map(approved) → OrderExecutor
 *                  → RecordTrades → SendRecapSMS
 */
export class ComputeStack extends Stack {
  public readonly vpc: IVpc;
  public readonly cluster: Cluster;
  public readonly ibGatewayService: FargateService;
  public readonly gatewayStateFileSystem: FileSystem;
  public readonly researchSfn: StateMachine;
  public readonly decideAndExecuteSfn: StateMachine;

  constructor(scope: Construct, id: string, props: ComputeStackProps) {
    super(scope, id, props);

    // ── VPC ─────────────────────────────────────────────────────────────────
    this.vpc = new Vpc(this, "Vpc", {
      natGateways: 0,
      availabilityZones: [props.availabilityZone],
      subnetConfiguration: [
        { name: "public", subnetType: SubnetType.PUBLIC, cidrMask: 24 },
      ],
      gatewayEndpoints: {
        DynamoDb: { service: GatewayVpcEndpointAwsService.DYNAMODB },
        S3: { service: GatewayVpcEndpointAwsService.S3 },
      },
    });
    // Interface endpoints — gateway endpoints exist only for DDB/S3; every
    // other AWS service called from in-VPC Lambdas needs an interface
    // endpoint (~$7.20/mo per AZ each).
    this.vpc.addInterfaceEndpoint("SecretsManagerEndpoint", {
      service: InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
      privateDnsEnabled: true,
    });
    this.vpc.addInterfaceEndpoint("BedrockRuntimeEndpoint", {
      service: InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
      privateDnsEnabled: true,
    });
    this.vpc.addInterfaceEndpoint("SnsEndpoint", {
      service: InterfaceVpcEndpointAwsService.SNS,
      privateDnsEnabled: true,
    });
    this.vpc.addInterfaceEndpoint("SqsEndpoint", {
      service: InterfaceVpcEndpointAwsService.SQS,
      privateDnsEnabled: true,
    });
    this.vpc.addInterfaceEndpoint("StepFunctionsEndpoint", {
      service: InterfaceVpcEndpointAwsService.STEP_FUNCTIONS,
      privateDnsEnabled: true,
    });

    // ── ECS cluster + service discovery ─────────────────────────────────────
    this.cluster = new Cluster(this, "Cluster", {
      vpc: this.vpc,
      clusterName: "astra-compute",
    });

    const dnsNamespace = new PrivateDnsNamespace(this, "ServiceDiscovery", {
      vpc: this.vpc,
      name: "astra.local",
    });

    // ── Lambda factory helpers ──────────────────────────────────────────────
    const pyLambda = (
      idStr: string,
      assetDir: string,
      opts: {
        memorySize?: number;
        timeout?: Duration;
        env?: Record<string, string>;
        inVpc?: boolean;
        bundling?: boolean;
        // SGs must be passed at construction time — calling
        // .connections.addSecurityGroup() after construction only mutates the
        // in-memory connections object, NOT the deployed Lambda's
        // vpcConfig.SecurityGroupIds.
        securityGroups?: SecurityGroup[];
      } = {},
    ): Function => {
      const codeAsset = opts.bundling
        ? Code.fromAsset(path.join(__dirname, "..", "lambdas", assetDir), {
            bundling: {
              image: Runtime.PYTHON_3_12.bundlingImage,
              platform: "linux/arm64",
              command: [
                "bash",
                "-c",
                "pip install --no-cache -r requirements.txt -t /asset-output && cp -au . /asset-output",
              ],
            },
          })
        : Code.fromAsset(path.join(__dirname, "..", "lambdas", assetDir));
      return new Function(this, idStr, {
        runtime: Runtime.PYTHON_3_12,
        architecture: Architecture.ARM_64,
        handler: "handler.handler",
        code: codeAsset,
        timeout: opts.timeout ?? Duration.seconds(60),
        memorySize: opts.memorySize ?? 256,
        environment: opts.env,
        ...(opts.inVpc
          ? {
              vpc: this.vpc,
              vpcSubnets: { subnetType: SubnetType.PUBLIC },
              allowPublicSubnet: true,
              ...(opts.securityGroups ? { securityGroups: opts.securityGroups } : {}),
            }
          : {}),
      });
    };

    // ── EarningsFetcher (Lambda) ────────────────────────────────────────────
    const earningsFetcher = pyLambda("EarningsFetcher", "earnings_fetcher", {
      timeout: Duration.seconds(60),
      memorySize: 256,
      env: {
        EARNINGS_CALENDAR_TABLE_NAME: props.earningsCalendarTable.tableName,
        DATA_FEED_SECRET_ARN: props.dataFeedCredentialsSecret.secretArn,
      },
    });
    props.earningsCalendarTable.grantWriteData(earningsFetcher);
    props.dataFeedCredentialsSecret.grantRead(earningsFetcher);

    // ── EFS for IB Gateway session/login persistence ────────────────────────
    const efsSg = new SecurityGroup(this, "EfsSg", {
      vpc: this.vpc,
      description: "Astra-ComputeStack EFS - NFS from Fargate tasks only",
      allowAllOutbound: true,
    });

    this.gatewayStateFileSystem = new FileSystem(this, "GatewayState", {
      vpc: this.vpc,
      vpcSubnets: { subnetType: SubnetType.PUBLIC },
      securityGroup: efsSg,
      performanceMode: PerformanceMode.GENERAL_PURPOSE,
      throughputMode: ThroughputMode.BURSTING,
      encrypted: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });

    const gatewayStateAp = new AccessPoint(this, "GatewayStateAp", {
      fileSystem: this.gatewayStateFileSystem,
      path: "/jts",
      createAcl: { ownerUid: "1000", ownerGid: "1000", permissions: "750" },
      posixUser: { uid: "1000", gid: "1000" },
    });

    // ── IB Gateway task ─────────────────────────────────────────────────────
    const gatewayLogs = new LogGroup(this, "GatewayLogs", {
      logGroupName: "/astra/compute/ib-gateway",
      retention: RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    const gatewayTaskDef = new FargateTaskDefinition(this, "GatewayTaskDef", {
      cpu: 1024,
      memoryLimitMiB: 4096,
      volumes: [
        {
          name: "gateway-state",
          efsVolumeConfiguration: {
            fileSystemId: this.gatewayStateFileSystem.fileSystemId,
            transitEncryption: "ENABLED",
            authorizationConfig: {
              accessPointId: gatewayStateAp.accessPointId,
              iam: "ENABLED",
            },
          },
        },
      ],
    });
    this.gatewayStateFileSystem.grantReadWrite(gatewayTaskDef.taskRole);
    gatewayTaskDef.taskRole.addToPrincipalPolicy(
      new PolicyStatement({
        actions: [
          "ssmmessages:CreateControlChannel",
          "ssmmessages:CreateDataChannel",
          "ssmmessages:OpenControlChannel",
          "ssmmessages:OpenDataChannel",
        ],
        resources: ["*"],
      }),
    );

    const gatewayContainer = gatewayTaskDef.addContainer("IbGateway", {
      image: ContainerImage.fromRegistry("gnzsnz/ib-gateway:stable"),
      logging: LogDrivers.awsLogs({ streamPrefix: "ib-gateway", logGroup: gatewayLogs }),
      environment: {
        READ_ONLY_API: "yes",
        TWS_ACCEPT_INCOMING: "accept",
        BYPASS_WARNING: "yes",
        AUTO_RESTART_TIME: "11:59 PM",
        TWOFA_TIMEOUT_ACTION: "restart",
        RELOGIN_AFTER_TWOFA_TIMEOUT: "yes",
        EXISTING_SESSION_DETECTED_ACTION: "primary",
        TWS_SETTINGS_PATH: "/home/ibgateway/tws_settings",
        TIME_ZONE: "America/New_York",
      },
      secrets: {
        TWS_USERID: EcsSecret.fromSecretsManager(props.ibkrCredentialsSecret, "username"),
        TWS_PASSWORD: EcsSecret.fromSecretsManager(props.ibkrCredentialsSecret, "password"),
        TRADING_MODE: EcsSecret.fromSecretsManager(props.ibkrCredentialsSecret, "trading_mode"),
      },
      portMappings: [
        { containerPort: 4003 }, // live (via socat)
        { containerPort: 4004 }, // paper (via socat)
      ],
    });
    gatewayContainer.addMountPoints({
      containerPath: "/home/ibgateway/tws_settings",
      sourceVolume: "gateway-state",
      readOnly: false,
    });

    const gatewaySg = new SecurityGroup(this, "GatewaySg", {
      vpc: this.vpc,
      description: "Astra-ComputeStack IB Gateway Fargate task",
      allowAllOutbound: true,
    });

    this.ibGatewayService = new FargateService(this, "IbGatewayService", {
      cluster: this.cluster,
      serviceName: "ib-gateway",
      taskDefinition: gatewayTaskDef,
      desiredCount: 1,
      assignPublicIp: true,
      vpcSubnets: { subnetType: SubnetType.PUBLIC },
      securityGroups: [gatewaySg],
      platformVersion: FargatePlatformVersion.LATEST,
      enableExecuteCommand: true,
      cloudMapOptions: {
        name: "ib-gateway",
        cloudMapNamespace: dnsNamespace,
        dnsRecordType: DnsRecordType.A,
      },
    });
    efsSg.addIngressRule(gatewaySg, Port.tcp(2049), "NFS from IB Gateway task");

    // Shared SG for any VPC-attached Lambda or Fargate task that calls IB
    // Gateway. Single SG keeps the Gateway ingress rule list short.
    const ibConsumerSg = new SecurityGroup(this, "IbConsumerSg", {
      vpc: this.vpc,
      description: "Astra-ComputeStack: workloads that talk to IB Gateway",
      allowAllOutbound: true,
    });
    gatewaySg.addIngressRule(ibConsumerSg, Port.tcp(4004), "IB Gateway (paper) from in-VPC consumers");
    gatewaySg.addIngressRule(ibConsumerSg, Port.tcp(4003), "IB Gateway (live)  from in-VPC consumers");

    // ── OptionsChainFetcher (Lambda in VPC, ib_async, one-per-candidate) ────
    const optionsChainFetcher = pyLambda("OptionsChainFetcher", "options_chain_fetcher", {
      timeout: Duration.minutes(5),
      memorySize: 1024,
      inVpc: true,
      bundling: true,
      securityGroups: [ibConsumerSg],
      env: {
        CONFIG_BUCKET_NAME: props.configBucket.bucketName,
        GATEWAY_HOST: "ib-gateway.astra.local",
        GATEWAY_PORT: "4004",
        // Base for randomized per-invocation clientId (Map fan-out collides
        // on a single clientId — IBKR rejects duplicates).
        GATEWAY_CLIENT_ID_BASE: "2000",
      },
    });
    props.configBucket.grantPut(optionsChainFetcher, "options-chains/*");

    // ── HistoricalMovesFetcher (Lambda in VPC, ib_async, per-ticker) ────────
    const historicalMovesFetcher = pyLambda("HistoricalMovesFetcher", "historical_moves_fetcher", {
      timeout: Duration.minutes(3),
      memorySize: 512,
      inVpc: true,
      bundling: true,
      securityGroups: [ibConsumerSg],
      env: {
        HISTORICAL_MOVES_TABLE_NAME: props.historicalMovesTable.tableName,
        GATEWAY_HOST: "ib-gateway.astra.local",
        GATEWAY_PORT: "4004",
        GATEWAY_CLIENT_ID_BASE: "1000",
      },
    });
    props.historicalMovesTable.grantWriteData(historicalMovesFetcher);
    // No Finnhub credentials needed — past_earnings_dates are supplied in
    // the SFN Map payload by EarningsFetcher (which lives outside the VPC
    // and can reach finnhub.io).

    // ── SentimentProcessor (Lambda in VPC, Bedrock) ─────────────────────────
    const sentimentProcessor = pyLambda("SentimentProcessor", "sentiment_processor", {
      timeout: Duration.minutes(5),
      memorySize: 512,
      inVpc: true,
      env: {
        SEEN_CONTENT_TABLE_NAME: props.seenContentTable.tableName,
        SENTIMENT_SCORES_TABLE_NAME: props.sentimentScoresTable.tableName,
        BEDROCK_MODEL_ID: "anthropic.claude-haiku-4-5-20251001-v1:0",
        BEDROCK_REGION: this.region,
      },
    });
    props.seenContentTable.grantReadData(sentimentProcessor);
    props.sentimentScoresTable.grantWriteData(sentimentProcessor);
    sentimentProcessor.addToRolePolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ["bedrock:InvokeModel"],
        resources: ["*"], // Bedrock model ARNs are accessible only by alias; "*" is the conventional grant
      }),
    );

    // ── StrategyEngine (Lambda in VPC, pyarrow Parquet reads) ───────────────
    const strategyEngine = pyLambda("StrategyEngine", "strategy_engine", {
      timeout: Duration.minutes(3),
      memorySize: 1024,
      inVpc: true,
      bundling: true,
      env: {
        HISTORICAL_MOVES_TABLE_NAME: props.historicalMovesTable.tableName,
        SENTIMENT_SCORES_TABLE_NAME: props.sentimentScoresTable.tableName,
        CONFIG_BUCKET_NAME: props.configBucket.bucketName,
        STRATEGY_CONFIG_KEY: "strategy.json",
        DEFAULT_CAPITAL_USD: "5000",
      },
    });
    props.historicalMovesTable.grantReadData(strategyEngine);
    props.sentimentScoresTable.grantReadData(strategyEngine);
    props.configBucket.grantRead(strategyEngine);

    // ── PreFlightChecks (Lambda in VPC) ─────────────────────────────────────
    const preFlightChecks = pyLambda("PreFlightChecks", "pre_flight_checks", {
      timeout: Duration.seconds(60),
      memorySize: 256,
      inVpc: true,
      env: {
        CONFIG_BUCKET_NAME: props.configBucket.bucketName,
        TRADE_LOG_TABLE_NAME: props.tradeLogTable.tableName,
        PENDING_TRADES_TABLE_NAME: props.pendingTradesTable.tableName,
        STOP_FILE_KEY: "STOP",
        EXECUTION_MODE_KEY: "execution-mode.json",
        MAX_TRADES_PER_DAY: "10",
        MAX_USD_PER_DAY: "10000",
        MAX_POSITIONS_PER_TICKER: "1",
        LATEST_ENTRY_TIME_ET: "15:55",
      },
    });
    props.configBucket.grantRead(preFlightChecks);
    props.tradeLogTable.grantReadData(preFlightChecks);
    props.pendingTradesTable.grantReadData(preFlightChecks);

    // ── OrderExecutor (Lambda in VPC, ib_async) ─────────────────────────────
    const orderExecutor = pyLambda("OrderExecutor", "order_executor", {
      timeout: Duration.minutes(3),
      memorySize: 512,
      inVpc: true,
      bundling: true,
      securityGroups: [ibConsumerSg],
      env: {
        PENDING_TRADES_TABLE_NAME: props.pendingTradesTable.tableName,
        GATEWAY_HOST: "ib-gateway.astra.local",
        GATEWAY_PORT: "4004",
        GATEWAY_CLIENT_ID_BASE: "3000",
        FILL_TIMEOUT_SECONDS: "60",
      },
    });
    props.pendingTradesTable.grantReadWriteData(orderExecutor);

    // ── RecordTrades (Lambda in VPC) ────────────────────────────────────────
    const recordTrades = pyLambda("RecordTrades", "record_trades", {
      timeout: Duration.minutes(2),
      memorySize: 512,
      inVpc: true,
      env: {
        TRADE_LOG_TABLE_NAME: props.tradeLogTable.tableName,
        PENDING_TRADES_TABLE_NAME: props.pendingTradesTable.tableName,
      },
    });
    props.tradeLogTable.grantWriteData(recordTrades);
    props.pendingTradesTable.grantWriteData(recordTrades);

    // ── SendRecapSMS (Lambda in VPC) ────────────────────────────────────────
    const sendRecapSms = pyLambda("SendRecapSMS", "send_recap_sms", {
      timeout: Duration.seconds(30),
      memorySize: 256,
      inVpc: true,
      env: {
        ALERTS_TOPIC_ARN: props.alertsTopic.topicArn,
      },
    });
    props.alertsTopic.grantPublish(sendRecapSms);

    // ── NewsRedditIngestion (Fargate, always-on) ────────────────────────────
    const ingestionLogs = new LogGroup(this, "IngestionLogs", {
      logGroupName: "/astra/compute/news-reddit-ingestion",
      retention: RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const ingestionTaskDef = new FargateTaskDefinition(this, "IngestionTaskDef", {
      cpu: 256,
      memoryLimitMiB: 512,
      runtimePlatform: {
        cpuArchitecture: CpuArchitecture.ARM64,
        operatingSystemFamily: OperatingSystemFamily.LINUX,
      },
    });
    ingestionTaskDef.addContainer("Ingestion", {
      image: ContainerImage.fromAsset(path.join(__dirname, "..", "services", "news_reddit_ingestion"), {
        platform: { platform: "linux/arm64" } as any,
      }),
      logging: LogDrivers.awsLogs({ streamPrefix: "ingestion", logGroup: ingestionLogs }),
      environment: {
        EARNINGS_CALENDAR_TABLE_NAME: props.earningsCalendarTable.tableName,
        SEEN_CONTENT_TABLE_NAME: props.seenContentTable.tableName,
        DATA_FEED_SECRET_ARN: props.dataFeedCredentialsSecret.secretArn,
        REDDIT_SECRET_ARN: props.redditCredentialsSecret.secretArn,
        SUBREDDITS: "wallstreetbets,options,stocks",
        TICKER_REFRESH_SECONDS: "900",
        INGESTION_TTL_DAYS: "3",
      },
    });
    props.earningsCalendarTable.grantReadData(ingestionTaskDef.taskRole);
    props.seenContentTable.grantReadWriteData(ingestionTaskDef.taskRole);
    props.dataFeedCredentialsSecret.grantRead(ingestionTaskDef.taskRole);
    props.redditCredentialsSecret.grantRead(ingestionTaskDef.taskRole);

    const ingestionSg = new SecurityGroup(this, "IngestionSg", {
      vpc: this.vpc,
      description: "Astra NewsRedditIngestion Fargate task",
      allowAllOutbound: true,
    });
    new FargateService(this, "IngestionService", {
      cluster: this.cluster,
      serviceName: "news-reddit-ingestion",
      taskDefinition: ingestionTaskDef,
      desiredCount: 1,
      assignPublicIp: true, // needs egress to Finnhub WS + Reddit
      vpcSubnets: { subnetType: SubnetType.PUBLIC },
      securityGroups: [ingestionSg],
      platformVersion: FargatePlatformVersion.LATEST,
    });

    // ── PositionMonitor (Fargate, always-on) ────────────────────────────────
    const monitorLogs = new LogGroup(this, "PositionMonitorLogs", {
      logGroupName: "/astra/compute/position-monitor",
      retention: RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const monitorTaskDef = new FargateTaskDefinition(this, "PositionMonitorTaskDef", {
      cpu: 256,
      memoryLimitMiB: 512,
      runtimePlatform: {
        cpuArchitecture: CpuArchitecture.ARM64,
        operatingSystemFamily: OperatingSystemFamily.LINUX,
      },
    });
    monitorTaskDef.addContainer("Monitor", {
      image: ContainerImage.fromAsset(path.join(__dirname, "..", "services", "position_monitor"), {
        platform: { platform: "linux/arm64" } as any,
      }),
      logging: LogDrivers.awsLogs({ streamPrefix: "monitor", logGroup: monitorLogs }),
      environment: {
        PENDING_TRADES_TABLE_NAME: props.pendingTradesTable.tableName,
        LIVE_POSITIONS_TABLE_NAME: props.livePositionsTable.tableName,
        ALERTS_TOPIC_ARN: props.alertsTopic.topicArn,
        GATEWAY_HOST: "ib-gateway.astra.local",
        GATEWAY_PORT: "4004",
        GATEWAY_CLIENT_ID: "31",
        POLL_INTERVAL_SECONDS: "60",
        HEARTBEAT_MAX_GAP_SECONDS: "600",
      },
    });
    props.pendingTradesTable.grantReadWriteData(monitorTaskDef.taskRole);
    props.livePositionsTable.grantReadWriteData(monitorTaskDef.taskRole);
    props.alertsTopic.grantPublish(monitorTaskDef.taskRole);

    new FargateService(this, "PositionMonitorService", {
      cluster: this.cluster,
      serviceName: "position-monitor",
      taskDefinition: monitorTaskDef,
      desiredCount: 1,
      assignPublicIp: true,
      vpcSubnets: { subnetType: SubnetType.PUBLIC },
      securityGroups: [ibConsumerSg],
      platformVersion: FargatePlatformVersion.LATEST,
    });

    // ── Research Step Function ──────────────────────────────────────────────
    // Earnings → Map(candidates) → HistoricalMovesFetcher.
    const earningsInvoke = new LambdaInvoke(this, "InvokeEarningsFetcher", {
      lambdaFunction: earningsFetcher,
      payload: TaskInput.fromObject({}),
      payloadResponseOnly: true,
      resultPath: "$.earnings",
    });

    const historicalMap = new SfnMap(this, "MapHistoricalMoves", {
      itemsPath: "$.earnings.candidates",
      maxConcurrency: 3,
      resultPath: "$.historicalResults",
    });
    historicalMap.itemProcessor(
      new LambdaInvoke(this, "InvokeHistoricalMovesFetcher", {
        lambdaFunction: historicalMovesFetcher,
        payloadResponseOnly: true,
      }).addRetry({
        errors: ["States.TaskFailed"],
        maxAttempts: 2,
        backoffRate: 2,
        interval: Duration.seconds(15),
      }).addCatch(
        new Pass(this, "HistoricalMovesIterCatch", {
          parameters: {
            "ticker.$": "$.ticker",
            "status": "error",
            "error.$": "$.Error",
            "cause.$": "$.Cause",
          },
        }),
        { errors: ["States.ALL"], resultPath: "$.error" },
      ),
    );

    const researchSuccess = new Succeed(this, "ResearchSucceed");
    const researchDefinition = earningsInvoke.next(historicalMap).next(researchSuccess);

    const researchLogs = new LogGroup(this, "ResearchSfnLogs", {
      logGroupName: "/astra/sfn/research",
      retention: RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    this.researchSfn = new StateMachine(this, "ResearchSfn", {
      stateMachineName: "astra-research",
      stateMachineType: StateMachineType.STANDARD,
      definitionBody: DefinitionBody.fromChainable(researchDefinition),
      timeout: Duration.hours(2),
      logs: { destination: researchLogs, level: LogLevel.ALL, includeExecutionData: true },
    });

    // ── DecideAndExecute Step Function ──────────────────────────────────────
    // Re-fetch the candidate set via EarningsFetcher so the decision run uses
    // today's actionable list, not a stale event.
    const decideEarningsInvoke = new LambdaInvoke(this, "InvokeEarningsFetcherDecide", {
      lambdaFunction: earningsFetcher,
      payload: TaskInput.fromObject({}),
      payloadResponseOnly: true,
      resultPath: "$.earnings",
    });

    // Parallel: fresh chains + fresh sentiment.
    const chainMap = new SfnMap(this, "MapOptionsChainFetcher", {
      itemsPath: "$.earnings.candidates",
      maxConcurrency: 3,
    });
    chainMap.itemProcessor(
      new LambdaInvoke(this, "InvokeOptionsChainFetcher", {
        lambdaFunction: optionsChainFetcher,
        payloadResponseOnly: true,
      }).addRetry({
        errors: ["States.TaskFailed"],
        maxAttempts: 2,
        backoffRate: 2,
        interval: Duration.seconds(15),
      }).addCatch(
        new Pass(this, "OptionsChainIterCatch", {
          parameters: {
            "ticker.$": "$.ticker",
            "status": "error",
            "error.$": "$.Error",
            "cause.$": "$.Cause",
          },
        }),
        { errors: ["States.ALL"], resultPath: "$.error" },
      ),
    );

    const sentimentInvoke = new LambdaInvoke(this, "InvokeSentimentProcessor", {
      lambdaFunction: sentimentProcessor,
      payload: TaskInput.fromObject({
        "target_T.$": "$.earnings.target_T",
        "candidates.$": "$.earnings.candidates",
        "window_hours": 24,
      }),
      payloadResponseOnly: true,
    });

    const parallelResearch = new Parallel(this, "ParallelChainAndSentiment", {
      resultPath: "$.research",
    })
      .branch(chainMap)
      .branch(sentimentInvoke);

    const strategyInvoke = new LambdaInvoke(this, "InvokeStrategyEngine", {
      lambdaFunction: strategyEngine,
      payload: TaskInput.fromObject({
        "target_T.$": "$.earnings.target_T",
        "candidates.$": "$.earnings.candidates",
        "chain_results.$": "$.research[0]",
        "sentiment_result.$": "$.research[1]",
      }),
      payloadResponseOnly: true,
      resultPath: "$.strategy",
    });

    const preFlightInvoke = new LambdaInvoke(this, "InvokePreFlightChecks", {
      lambdaFunction: preFlightChecks,
      payload: TaskInput.fromObject({
        "target_T.$": "$.earnings.target_T",
        "picks.$": "$.strategy.picks",
      }),
      payloadResponseOnly: true,
      resultPath: "$.preflight",
    });

    // Live branch: Map(approved) → OrderExecutor.
    const orderMap = new SfnMap(this, "MapOrderExecutor", {
      itemsPath: "$.preflight.approved_picks",
      maxConcurrency: 3,
      itemSelector: {
        "trade_id.$": "$$.Map.Item.Value.trade_id",
        "ticker.$": "$$.Map.Item.Value.ticker",
        "side.$": "$$.Map.Item.Value.side",
        "qty.$": "$$.Map.Item.Value.qty",
        "limit_price.$": "$$.Map.Item.Value.limit_price",
        "contract.$": "$$.Map.Item.Value.contract",
        "report_timing.$": "$$.Map.Item.Value.report_timing",
        "earnings_date.$": "$$.Map.Item.Value.earnings_date",
        "execution_mode.$": "$.preflight.execution_mode",
      },
      resultPath: "$.order_results",
    });
    orderMap.itemProcessor(
      new LambdaInvoke(this, "InvokeOrderExecutor", {
        lambdaFunction: orderExecutor,
        payloadResponseOnly: true,
      }).addRetry({
        errors: ["States.TaskFailed", "Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException"],
        maxAttempts: 1, // do NOT auto-retry — duplicate-trade-id sentinel handles it but visibility wins
        interval: Duration.seconds(5),
      }).addCatch(
        new Pass(this, "OrderExecutorIterCatch", {
          parameters: {
            "trade_id.$": "$.trade_id",
            "ticker.$": "$.ticker",
            "status": "error",
            "error.$": "$.Error",
            "cause.$": "$.Cause",
          },
        }),
        { errors: ["States.ALL"], resultPath: "$.error" },
      ),
    );

    const recordTradesInvoke = new LambdaInvoke(this, "InvokeRecordTrades", {
      lambdaFunction: recordTrades,
      payload: TaskInput.fromObject({
        "target_T.$": "$.earnings.target_T",
        "execution_mode.$": "$.preflight.execution_mode",
        "approved_picks.$": "$.preflight.approved_picks",
        "rejected.$": "$.preflight.rejected",
        "order_results.$": "$.order_results",
      }),
      payloadResponseOnly: true,
      resultPath: "$.recorded",
    });

    const recordTradesRecommendInvoke = new LambdaInvoke(this, "InvokeRecordTradesRecommend", {
      lambdaFunction: recordTrades,
      payload: TaskInput.fromObject({
        "target_T.$": "$.earnings.target_T",
        "execution_mode.$": "$.preflight.execution_mode",
        "approved_picks.$": "$.preflight.approved_picks",
        "rejected.$": "$.preflight.rejected",
        "order_results": [],
      }),
      payloadResponseOnly: true,
      resultPath: "$.recorded",
    });

    const smsInvokeLive = new LambdaInvoke(this, "InvokeSendRecapSMSLive", {
      lambdaFunction: sendRecapSms,
      payload: TaskInput.fromObject({
        "target_T.$": "$.earnings.target_T",
        "execution_mode.$": "$.preflight.execution_mode",
        "stop_file_active.$": "$.preflight.stop_file_active",
        "past_cutoff.$": "$.preflight.past_cutoff",
        "approved_picks.$": "$.preflight.approved_picks",
        "rejected.$": "$.preflight.rejected",
        "order_results.$": "$.order_results",
      }),
      payloadResponseOnly: true,
    });

    const smsInvokeRecommend = new LambdaInvoke(this, "InvokeSendRecapSMSRecommend", {
      lambdaFunction: sendRecapSms,
      payload: TaskInput.fromObject({
        "target_T.$": "$.earnings.target_T",
        "execution_mode.$": "$.preflight.execution_mode",
        "stop_file_active.$": "$.preflight.stop_file_active",
        "past_cutoff.$": "$.preflight.past_cutoff",
        "approved_picks.$": "$.preflight.approved_picks",
        "rejected.$": "$.preflight.rejected",
      }),
      payloadResponseOnly: true,
    });

    const decideSuccess = new Succeed(this, "DecideAndExecuteSucceed");

    const liveBranch = orderMap.next(recordTradesInvoke).next(smsInvokeLive).next(decideSuccess);
    const recommendBranch = recordTradesRecommendInvoke.next(smsInvokeRecommend).next(decideSuccess);

    const modeChoice = new Choice(this, "ChoiceExecutionMode")
      .when(Condition.stringEquals("$.preflight.execution_mode", "live"), liveBranch)
      .otherwise(recommendBranch);

    const decideDefinition = decideEarningsInvoke
      .next(parallelResearch)
      .next(strategyInvoke)
      .next(preFlightInvoke)
      .next(modeChoice);

    const decideLogs = new LogGroup(this, "DecideAndExecuteSfnLogs", {
      logGroupName: "/astra/sfn/decide-and-execute",
      retention: RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    this.decideAndExecuteSfn = new StateMachine(this, "DecideAndExecuteSfn", {
      stateMachineName: "astra-decide-and-execute",
      stateMachineType: StateMachineType.STANDARD,
      definitionBody: DefinitionBody.fromChainable(decideDefinition),
      timeout: Duration.minutes(30),
      logs: { destination: decideLogs, level: LogLevel.ALL, includeExecutionData: true },
    });

    // ── EventBridge schedules ───────────────────────────────────────────────
    // Research run @ 06:00 UTC ≈ 01:00 ET (handles DST: runs at 01:00 EST
    // or 02:00 EDT — both well before market open, both fine).
    new Rule(this, "ResearchSchedule", {
      ruleName: "astra-research-schedule",
      schedule: Schedule.cron({ minute: "0", hour: "6" }),
      targets: [new SfnStateMachine(this.researchSfn)],
    });

    // Decide-and-execute @ 19:00 UTC = 14:00 ET (winter) / 15:00 ET (summer).
    // To pin 15:00 ET year-round we'd need two rules (one EST, one EDT);
    // for now we use 19:00 UTC and accept the 1-hour shift across DST.
    // CLAUDE.md says "final ~30 min before close" — 15:00 ET is 1h pre-close,
    // 14:00 ET is 2h pre-close. PreFlightChecks enforces the hard 15:55 ET
    // cutoff so an early run on EST doesn't leak orders past the window.
    new Rule(this, "DecideAndExecuteSchedule", {
      ruleName: "astra-decide-and-execute-schedule",
      schedule: Schedule.cron({ minute: "0", hour: "19" }),
      targets: [new SfnStateMachine(this.decideAndExecuteSfn)],
    });

    // ── Outputs ─────────────────────────────────────────────────────────────
    new CfnOutput(this, "ClusterName", { value: this.cluster.clusterName });
    new CfnOutput(this, "IbGatewayServiceName", { value: this.ibGatewayService.serviceName });
    new CfnOutput(this, "GatewayLogGroup", { value: gatewayLogs.logGroupName });
    new CfnOutput(this, "EfsFileSystemId", { value: this.gatewayStateFileSystem.fileSystemId });
    new CfnOutput(this, "EarningsFetcherFunctionName", { value: earningsFetcher.functionName });
    new CfnOutput(this, "OptionsChainFetcherFunctionName", { value: optionsChainFetcher.functionName });
    new CfnOutput(this, "HistoricalMovesFetcherFunctionName", { value: historicalMovesFetcher.functionName });
    new CfnOutput(this, "SentimentProcessorFunctionName", { value: sentimentProcessor.functionName });
    new CfnOutput(this, "StrategyEngineFunctionName", { value: strategyEngine.functionName });
    new CfnOutput(this, "PreFlightChecksFunctionName", { value: preFlightChecks.functionName });
    new CfnOutput(this, "OrderExecutorFunctionName", { value: orderExecutor.functionName });
    new CfnOutput(this, "RecordTradesFunctionName", { value: recordTrades.functionName });
    new CfnOutput(this, "SendRecapSMSFunctionName", { value: sendRecapSms.functionName });
    new CfnOutput(this, "ResearchSfnArn", { value: this.researchSfn.stateMachineArn });
    new CfnOutput(this, "DecideAndExecuteSfnArn", { value: this.decideAndExecuteSfn.stateMachineArn });
    new CfnOutput(this, "IngestionLogGroup", { value: ingestionLogs.logGroupName });
    new CfnOutput(this, "PositionMonitorLogGroup", { value: monitorLogs.logGroupName });
    new CfnOutput(this, "GatewayInternalDnsName", { value: "ib-gateway.astra.local" });
    new CfnOutput(this, "EcsExecShellCommand", {
      value: `TASK=$(aws ecs list-tasks --cluster astra-compute --service-name ib-gateway --query "taskArns[0]" --output text) && aws ecs execute-command --cluster astra-compute --task "$TASK" --container IbGateway --interactive --command "/bin/bash"`,
    });
    new CfnOutput(this, "GatewayLogTailCommand", {
      value: `aws logs tail ${gatewayLogs.logGroupName} --follow`,
    });
  }
}
