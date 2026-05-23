import {
  Stack,
  StackProps,
  RemovalPolicy,
  CfnOutput,
} from "aws-cdk-lib";
import { Construct } from "constructs";
import {
  Vpc,
  SubnetType,
  SecurityGroup,
  Port,
  IVpc,
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
import { Secret } from "aws-cdk-lib/aws-secretsmanager";
import { LogGroup, RetentionDays } from "aws-cdk-lib/aws-logs";
import { PolicyStatement } from "aws-cdk-lib/aws-iam";

export interface ExecutionStackProps extends StackProps {
  /** Secret with paper-account creds {username, password, account_id, trading_mode}. */
  readonly ibkrCredentialsSecret: Secret;
  /** AZ for the VPC + EFS. Single-AZ keeps things simple and avoids cross-AZ EFS cost. */
  readonly availabilityZone: string;
}

/**
 * ExecutionStack — IB Gateway running as a Fargate task using the
 * gnzsnz/ib-gateway community image. Money-at-risk layer per CLAUDE.md §5.
 *
 * Replaces the previous EC2+IBC bare-metal install; the image bundles IB Gateway,
 * IBC, Xvfb, socat, and the auto-restart logic. EFS persists ~/Jts (session tokens,
 * jts.ini) across task replacements so 2FA isn't required on every restart.
 *
 * The image runs IB Gateway on internal ports 4001 (live) / 4002 (paper), then
 * socat republishes them on 4003 / 4004. So external clients connect on those
 * ports — NOT the standard 4001/4002. See CLAUDE.md §6 for the port table.
 *
 * OrderExecutor and PositionMonitor are NOT in this stack yet; they'll arrive
 * as separate Fargate tasks (or sidecars) once Gateway is verified up.
 */
export class ExecutionStack extends Stack {
  public readonly vpc: IVpc;
  public readonly cluster: Cluster;
  public readonly service: FargateService;
  public readonly fileSystem: FileSystem;

  constructor(scope: Construct, id: string, props: ExecutionStackProps) {
    super(scope, id, props);

    // ── VPC: single public subnet, no NAT. ──
    this.vpc = new Vpc(this, "Vpc", {
      natGateways: 0,
      availabilityZones: [props.availabilityZone],
      subnetConfiguration: [
        { name: "public", subnetType: SubnetType.PUBLIC, cidrMask: 24 },
      ],
    });

    // ── EFS for persistent ~/Jts (session tokens, settings) ──
    // Mount target in our single AZ. POSIX access point pins UID/GID 1000
    // (matches the ibgateway user inside the gnzsnz image).
    const efsSg = new SecurityGroup(this, "EfsSg", {
      vpc: this.vpc,
      description: "Astra EFS - NFS access from Fargate task SG only",
      allowAllOutbound: true,
    });

    this.fileSystem = new FileSystem(this, "GatewayState", {
      vpc: this.vpc,
      vpcSubnets: { subnetType: SubnetType.PUBLIC },
      securityGroup: efsSg,
      performanceMode: PerformanceMode.GENERAL_PURPOSE,
      throughputMode: ThroughputMode.BURSTING,
      encrypted: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });

    const accessPoint = new AccessPoint(this, "GatewayStateAp", {
      fileSystem: this.fileSystem,
      path: "/jts",
      createAcl: { ownerUid: "1000", ownerGid: "1000", permissions: "750" },
      posixUser: { uid: "1000", gid: "1000" },
    });

    // ── CloudWatch log group for the container ──
    const logGroup = new LogGroup(this, "ServiceLogs", {
      logGroupName: "/astra/execution/ib-gateway",
      retention: RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // ── ECS cluster + task ──
    // Pin cluster + service names so the ecs-exec / log-tail commands in the
    // stack outputs are stable, short, and copy-pasteable.
    this.cluster = new Cluster(this, "Cluster", {
      vpc: this.vpc,
      clusterName: "astra-execution",
    });

    const taskDef = new FargateTaskDefinition(this, "TaskDef", {
      cpu: 1024,
      memoryLimitMiB: 4096,
      volumes: [
        {
          name: "gateway-state",
          efsVolumeConfiguration: {
            fileSystemId: this.fileSystem.fileSystemId,
            transitEncryption: "ENABLED",
            authorizationConfig: {
              accessPointId: accessPoint.accessPointId,
              iam: "ENABLED",
            },
          },
        },
      ],
    });

    // Task needs ClientMount + ClientWrite via IAM (efsVolumeConfiguration.iam=ENABLED).
    // The access point's PosixUser/CreateAcl handle file ownership; no separate grant.
    this.fileSystem.grantReadWrite(taskDef.taskRole);

    // ECS Exec ("aws ecs execute-command") requires the task role to be able to
    // open SSM messaging channels. CDK's enableExecuteCommand: true should add
    // these automatically, but the behavior has varied across CDK versions —
    // grant them explicitly so a shell is always available for debugging.
    taskDef.taskRole.addToPrincipalPolicy(new PolicyStatement({
      actions: [
        "ssmmessages:CreateControlChannel",
        "ssmmessages:CreateDataChannel",
        "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel",
      ],
      resources: ["*"],
    }));

    const container = taskDef.addContainer("IbGateway", {
      image: ContainerImage.fromRegistry("gnzsnz/ib-gateway:stable"),
      logging: LogDrivers.awsLogs({
        streamPrefix: "ib-gateway",
        logGroup,
      }),
      environment: {
        // Recommend-mode hardware guard per CLAUDE.md §6 — Gateway will reject
        // any order submission even if execution_mode in our code says otherwise.
        READ_ONLY_API: "yes",
        // Accept connections from outside localhost (we connect from other AWS
        // resources later — PipelineStack Lambdas, monitor, etc.).
        TWS_ACCEPT_INCOMING: "accept",
        // Bypass non-fatal IBKR confirmation dialogs.
        BYPASS_WARNING: "yes",
        // Daily restart at midnight ET; avoids the daily 2FA push.
        AUTO_RESTART_TIME: "11:59 PM",
        TWOFA_TIMEOUT_ACTION: "restart",
        RELOGIN_AFTER_TWOFA_TIMEOUT: "yes",
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

    container.addMountPoints({
      containerPath: "/home/ibgateway/tws_settings",
      sourceVolume: "gateway-state",
      readOnly: false,
    });

    // ── Service ──
    const serviceSg = new SecurityGroup(this, "ServiceSg", {
      vpc: this.vpc,
      description: "Astra-ExecutionStack IB Gateway Fargate task",
      allowAllOutbound: true,
    });

    this.service = new FargateService(this, "Service", {
      cluster: this.cluster,
      serviceName: "ib-gateway",
      taskDefinition: taskDef,
      desiredCount: 1,
      assignPublicIp: true,
      vpcSubnets: { subnetType: SubnetType.PUBLIC },
      securityGroups: [serviceSg],
      platformVersion: FargatePlatformVersion.LATEST,
      enableExecuteCommand: true, // allow `aws ecs execute-command` for debugging
    });

    // Allow the task SG to reach EFS on NFS port.
    efsSg.addIngressRule(serviceSg, Port.tcp(2049), "NFS from Fargate task");

    // Outputs
    new CfnOutput(this, "ClusterName", { value: this.cluster.clusterName });
    new CfnOutput(this, "ServiceName", { value: this.service.serviceName });
    new CfnOutput(this, "LogGroupName", { value: logGroup.logGroupName });
    new CfnOutput(this, "EfsFileSystemId", { value: this.fileSystem.fileSystemId });
    new CfnOutput(this, "EcsExecShellCommand", {
      value: `TASK=$(aws ecs list-tasks --cluster astra-execution --service-name ib-gateway --query "taskArns[0]" --output text) && aws ecs execute-command --cluster astra-execution --task "$TASK" --container IbGateway --interactive --command "/bin/bash"`,
    });
    new CfnOutput(this, "LogTailCommand", {
      value: `aws logs tail ${logGroup.logGroupName} --follow`,
    });
  }
}
