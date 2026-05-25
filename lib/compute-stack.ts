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
} from "aws-cdk-lib/aws-ec2";
import {
  Cluster,
  FargateTaskDefinition,
  FargateService,
  ContainerImage,
  LogDrivers,
  Secret as EcsSecret,
  FargatePlatformVersion,
} from "aws-cdk-lib/aws-ecs";
import {
  FileSystem,
  PerformanceMode,
  ThroughputMode,
  AccessPoint,
} from "aws-cdk-lib/aws-efs";
import { Function, Runtime, Code, Architecture } from "aws-cdk-lib/aws-lambda";
import { Rule, Schedule } from "aws-cdk-lib/aws-events";
import { LambdaFunction } from "aws-cdk-lib/aws-events-targets";
import { Table } from "aws-cdk-lib/aws-dynamodb";
import { Secret } from "aws-cdk-lib/aws-secretsmanager";
import { Bucket } from "aws-cdk-lib/aws-s3";
import { LogGroup, RetentionDays } from "aws-cdk-lib/aws-logs";
import { PolicyStatement } from "aws-cdk-lib/aws-iam";
import { PrivateDnsNamespace, DnsRecordType } from "aws-cdk-lib/aws-servicediscovery";

export interface ComputeStackProps extends StackProps {
  /** earnings_calendar table from CoreStack — EarningsFetcher writes here. */
  readonly earningsCalendarTable: Table;
  /** historical_moves table from CoreStack — OptionsChainFetcher writes here. */
  readonly historicalMovesTable: Table;
  /** Finnhub / Polygon API keys from CoreStack — read by data fetchers. */
  readonly dataFeedCredentialsSecret: Secret;
  /** IBKR credentials from CoreStack — injected into the Gateway Fargate task. */
  readonly ibkrCredentialsSecret: Secret;
  /** Config + chain-snapshot bucket from CoreStack. */
  readonly configBucket: Bucket;
  /** Single AZ for the VPC + EFS. Keeps things simple and avoids cross-AZ NFS cost. */
  readonly availabilityZone: string;
}

/**
 * ComputeStack — everything stateless. Combines what were previously the
 * PipelineStack (research / decision brain) and ExecutionStack (money layer)
 * into one stack with a shared VPC + ECS cluster. See CLAUDE.md §5 for the
 * two-stack decision.
 *
 * Money-safety is enforced at the construct level (per-task IAM, the STOP file
 * in CoreStack's S3 bucket, READ_ONLY_API=yes on the Gateway container, and
 * the execution_mode=recommend config flag on OrderExecutor when it lands) —
 * not at the stack boundary.
 *
 * Current components:
 *   - IB Gateway Fargate service (gnzsnz/ib-gateway:stable, EFS-persisted)
 *   - EarningsFetcher Lambda (nightly Finnhub calendar → earnings_calendar)
 *
 * Pending:
 *   - OptionsChainFetcher  — reaches Gateway via VPC-internal address
 *   - NewsRedditIngestion  — Fargate WebSocket listener + Reddit Lambda
 *   - SentimentProcessor   — Bedrock claude-haiku-4-5 batch
 *   - StrategyEngine       — the scorer
 *   - OrderExecutor        — Fargate task, submits via ib_async
 *   - PositionMonitor      — Fargate task, polls quotes, fires SMS alerts
 */
export class ComputeStack extends Stack {
  public readonly vpc: IVpc;
  public readonly cluster: Cluster;
  public readonly ibGatewayService: FargateService;
  public readonly gatewayStateFileSystem: FileSystem;

  constructor(scope: Construct, id: string, props: ComputeStackProps) {
    super(scope, id, props);

    // ── VPC: single public subnet, no NAT. Shared by all Fargate tasks and
    // VPC-attached Lambdas in this stack. ────────────────────────────────────
    this.vpc = new Vpc(this, "Vpc", {
      natGateways: 0,
      availabilityZones: [props.availabilityZone],
      subnetConfiguration: [
        { name: "public", subnetType: SubnetType.PUBLIC, cidrMask: 24 },
      ],
      // Gateway endpoints for DynamoDB + S3 so VPC-attached Lambdas (e.g.
      // OptionsChainFetcher) can reach those services over the AWS backbone
      // without needing a NAT Gateway. Free; routed via the subnet's route
      // table. Required because in-VPC Lambdas don't get public IPs even in
      // a public subnet, so they can't hit the public AWS endpoints.
      gatewayEndpoints: {
        DynamoDb: { service: GatewayVpcEndpointAwsService.DYNAMODB },
        S3: { service: GatewayVpcEndpointAwsService.S3 },
      },
    });

    // ── ECS cluster — one cluster, all Fargate workloads land here. ─────────
    this.cluster = new Cluster(this, "Cluster", {
      vpc: this.vpc,
      clusterName: "astra-compute",
    });

    // ── Private DNS namespace for service discovery ─────────────────────────
    // Gives the IB Gateway task a stable internal DNS name (`ib-gateway.astra.local`)
    // that Lambdas in this VPC resolve to the task's current private IP. Solves the
    // Fargate-task-IP-changes-on-replacement problem cleanly.
    const dnsNamespace = new PrivateDnsNamespace(this, "ServiceDiscovery", {
      vpc: this.vpc,
      name: "astra.local",
    });

    // ── EarningsFetcher (Lambda) ────────────────────────────────────────────
    // Nightly Finnhub earnings calendar → earnings_calendar table. Pure
    // stdlib + boto3 (no pip deps, no bundling).
    const earningsFetcher = new Function(this, "EarningsFetcher", {
      runtime: Runtime.PYTHON_3_12,
      architecture: Architecture.ARM_64,
      handler: "handler.handler",
      code: Code.fromAsset(path.join(__dirname, "..", "lambdas", "earnings_fetcher")),
      timeout: Duration.seconds(60),
      memorySize: 256,
      environment: {
        EARNINGS_CALENDAR_TABLE_NAME: props.earningsCalendarTable.tableName,
        DATA_FEED_SECRET_ARN: props.dataFeedCredentialsSecret.secretArn,
      },
    });
    props.earningsCalendarTable.grantWriteData(earningsFetcher);
    props.dataFeedCredentialsSecret.grantRead(earningsFetcher);

    // Nightly at 04:00 UTC = 8pm PST / 9pm PDT. DST drift is fine here — the
    // research cycle runs many hours before the next-day entry window, and
    // EventBridge doesn't do timezones for cron expressions.
    new Rule(this, "EarningsFetcherSchedule", {
      schedule: Schedule.cron({ minute: "0", hour: "4" }),
      targets: [new LambdaFunction(earningsFetcher)],
    });

    // ── EFS for IB Gateway session/login persistence ────────────────────────
    // Mount target in our single AZ. POSIX access point pins UID/GID 1000
    // (matches the ibgateway user inside the gnzsnz image). Survives task
    // replacements so session tokens persist and 2FA isn't re-prompted.
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

    // ── IB Gateway task definition + container ──────────────────────────────
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

    // Task needs ClientMount + ClientWrite via IAM (efsVolumeConfiguration.iam=ENABLED).
    // The access point's PosixUser/CreateAcl handle file ownership; no separate grant.
    this.gatewayStateFileSystem.grantReadWrite(gatewayTaskDef.taskRole);

    // ECS Exec ("aws ecs execute-command") requires the task role to be able to
    // open SSM messaging channels. CDK's enableExecuteCommand: true should add
    // these automatically, but the behavior has varied across CDK versions —
    // grant them explicitly so a shell is always available for debugging.
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
        // Recommend-mode hardware guard per CLAUDE.md §6 — Gateway will reject
        // any order submission even if execution_mode in our code says otherwise.
        READ_ONLY_API: "yes",
        // Accept connections from outside localhost (Lambdas / sibling Fargate
        // tasks in this same VPC connect via the task's private address).
        TWS_ACCEPT_INCOMING: "accept",
        // Bypass non-fatal IBKR confirmation dialogs.
        BYPASS_WARNING: "yes",
        // Daily restart at midnight ET; avoids the daily 2FA push.
        AUTO_RESTART_TIME: "11:59 PM",
        TWOFA_TIMEOUT_ACTION: "restart",
        RELOGIN_AFTER_TWOFA_TIMEOUT: "yes",
        // After the daily restart, IBKR sometimes shows an "Existing session
        // detected" dialog because the old session hasn't expired server-side.
        // "primary" tells Gateway to take over as the live session and kick
        // the phantom one — without this, IBC hangs on the dialog waiting
        // for a human click. The image README says this defaults to "primary"
        // but the actual default is empty (= manual). Set explicitly.
        EXISTING_SESSION_DETECTED_ACTION: "primary",
        // Pin TWS settings path to the mounted EFS volume. Use a separate path
        // (NOT /home/ibgateway/Jts) so we don't shadow the jts.ini.tmpl and
        // config templates the image bundles at the default location.
        TWS_SETTINGS_PATH: "/home/ibgateway/tws_settings",
        TIME_ZONE: "America/New_York",
      },
      secrets: {
        TWS_USERID: EcsSecret.fromSecretsManager(props.ibkrCredentialsSecret, "username"),
        TWS_PASSWORD: EcsSecret.fromSecretsManager(props.ibkrCredentialsSecret, "password"),
        TRADING_MODE: EcsSecret.fromSecretsManager(props.ibkrCredentialsSecret, "trading_mode"),
      },
      portMappings: [
        { containerPort: 4003 }, // live (via socat) — exposed but unused while paper
        { containerPort: 4004 }, // paper (via socat) — the one we use during recommend-only
      ],
    });

    gatewayContainer.addMountPoints({
      containerPath: "/home/ibgateway/tws_settings",
      sourceVolume: "gateway-state",
      readOnly: false,
    });

    // ── IB Gateway service ──────────────────────────────────────────────────
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
      // Register the task in the private DNS namespace so Lambdas resolve
      // ib-gateway.astra.local → current task private IP.
      cloudMapOptions: {
        name: "ib-gateway",
        cloudMapNamespace: dnsNamespace,
        dnsRecordType: DnsRecordType.A,
      },
    });

    // Allow the Gateway task SG to reach EFS on NFS port.
    efsSg.addIngressRule(gatewaySg, Port.tcp(2049), "NFS from IB Gateway task");

    // ── OptionsChainFetcher (Lambda in VPC) ─────────────────────────────────
    // Reads today's actionable candidates from earnings_calendar, connects to
    // IB Gateway via VPC-internal DNS (Cloud Map), pulls each candidate's ATM
    // straddle chain, computes implied_move, writes historical_moves + chain
    // snapshot to S3. Needs `ib_async` (pip) — bundled at synth via Docker.
    const optionsChainFetcherSg = new SecurityGroup(this, "OptionsChainFetcherSg", {
      vpc: this.vpc,
      description: "Astra-ComputeStack OptionsChainFetcher Lambda",
      allowAllOutbound: true,
    });

    const optionsChainFetcher = new Function(this, "OptionsChainFetcher", {
      runtime: Runtime.PYTHON_3_12,
      // ARM64 (Graviton2): ~20% cheaper than x86_64, no Rosetta emulation
      // on Apple Silicon during bundling, and native end-to-end (Mac arm64 →
      // Docker arm64 → pip arm64 wheels → Lambda arm64). See CLAUDE.md §4.
      architecture: Architecture.ARM_64,
      handler: "handler.handler",
      // Docker bundling — canonical CDK pattern for Python Lambdas with pip deps.
      // platform: "linux/arm64" forces Docker to pull the arm64 variant of the
      // SAM build image regardless of host arch, so bundling produces arm64
      // wheels matching the Lambda runtime. Requires Docker Desktop running.
      code: Code.fromAsset(path.join(__dirname, "..", "lambdas", "options_chain_fetcher"), {
        bundling: {
          image: Runtime.PYTHON_3_12.bundlingImage,
          platform: "linux/arm64",
          command: [
            "bash", "-c",
            "pip install --no-cache -r requirements.txt -t /asset-output && cp -au . /asset-output",
          ],
        },
      }),
      timeout: Duration.minutes(5),
      memorySize: 512,
      vpc: this.vpc,
      vpcSubnets: { subnetType: SubnetType.PUBLIC },
      securityGroups: [optionsChainFetcherSg],
      allowPublicSubnet: true,
      environment: {
        EARNINGS_CALENDAR_TABLE_NAME: props.earningsCalendarTable.tableName,
        HISTORICAL_MOVES_TABLE_NAME: props.historicalMovesTable.tableName,
        CONFIG_BUCKET_NAME: props.configBucket.bucketName,
        GATEWAY_HOST: "ib-gateway.astra.local",
        GATEWAY_PORT: "4004", // paper; flip to 4003 for live alongside trading_mode change
        GATEWAY_CLIENT_ID: "10",
      },
    });
    props.earningsCalendarTable.grantReadData(optionsChainFetcher);
    props.historicalMovesTable.grantWriteData(optionsChainFetcher);
    props.configBucket.grantPut(optionsChainFetcher, "options-chains/*");

    // Allow Lambda ENI to reach Gateway on the paper socat port.
    gatewaySg.addIngressRule(
      optionsChainFetcherSg,
      Port.tcp(4004),
      "OptionsChainFetcher Lambda to IB Gateway (paper)",
    );

    // Schedule 30 min after EarningsFetcher (04:30 UTC) so candidate rows
    // are guaranteed in place when this runs.
    new Rule(this, "OptionsChainFetcherSchedule", {
      schedule: Schedule.cron({ minute: "30", hour: "4" }),
      targets: [new LambdaFunction(optionsChainFetcher)],
    });

    // ── Outputs ─────────────────────────────────────────────────────────────
    new CfnOutput(this, "ClusterName", { value: this.cluster.clusterName });
    new CfnOutput(this, "IbGatewayServiceName", { value: this.ibGatewayService.serviceName });
    new CfnOutput(this, "GatewayLogGroup", { value: gatewayLogs.logGroupName });
    new CfnOutput(this, "EfsFileSystemId", { value: this.gatewayStateFileSystem.fileSystemId });
    new CfnOutput(this, "EarningsFetcherFunctionName", { value: earningsFetcher.functionName });
    new CfnOutput(this, "OptionsChainFetcherFunctionName", { value: optionsChainFetcher.functionName });
    new CfnOutput(this, "GatewayInternalDnsName", { value: "ib-gateway.astra.local" });
    new CfnOutput(this, "EcsExecShellCommand", {
      value: `TASK=$(aws ecs list-tasks --cluster astra-compute --service-name ib-gateway --query "taskArns[0]" --output text) && aws ecs execute-command --cluster astra-compute --task "$TASK" --container IbGateway --interactive --command "/bin/bash"`,
    });
    new CfnOutput(this, "GatewayLogTailCommand", {
      value: `aws logs tail ${gatewayLogs.logGroupName} --follow`,
    });
  }
}
