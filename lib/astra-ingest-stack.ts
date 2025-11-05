import { Stack, StackProps } from "aws-cdk-lib";
import { Construct } from "constructs";
import { Queue } from "aws-cdk-lib/aws-sqs";
import { Secret } from "aws-cdk-lib/aws-secretsmanager";
import {
  Vpc,
  SubnetType,
  SecurityGroup,
  Peer,
  Port,
} from "aws-cdk-lib/aws-ec2";
import {
  Cluster,
  FargateService,
  FargateTaskDefinition,
  ContainerImage,
  LogDriver,
  AwsLogDriverMode,
  Secret as EcsSecret,
} from "aws-cdk-lib/aws-ecs";
import * as path from "path";
import { Platform } from "aws-cdk-lib/aws-ecr-assets";

interface AstraIngestStackProps extends StackProps {
  queue: Queue;
  finnhubSecret: Secret;
}

export class AstraIngestStack extends Stack {
  constructor(scope: Construct, id: string, props: AstraIngestStackProps) {
    super(scope, id, props);

    //
    // 1️⃣ VPC
    // A lightweight VPC with only public subnets.
    // Fargate runs inside it with a public IP for outbound HTTPS access.
    //
    const vpc = new Vpc(this, "AstraVpc", {
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [
        {
          name: "public",
          subnetType: SubnetType.PUBLIC,
        },
      ],
    });

    //
    // 2️⃣ ECS Cluster
    //
    const cluster = new Cluster(this, "AstraCluster", { vpc });

    //
    // 3️⃣ Task Definition
    // One container that maintains the WebSocket connection.
    //
    const task = new FargateTaskDefinition(this, "FeedTask", {
      cpu: 256, // 0.25 vCPU
      memoryLimitMiB: 512, // 0.5 GB
    });

    task.addContainer("FeedListener", {
      image: ContainerImage.fromAsset(
        path.join(__dirname, "../services/feed-listener"),
        {
          platform: Platform.LINUX_AMD64,
        }
      ),
      logging: LogDriver.awsLogs({
        streamPrefix: "astra-feed",
        mode: AwsLogDriverMode.NON_BLOCKING,
      }),
      environment: {
        QUEUE_URL: props.queue.queueUrl,
        PROVIDER: "finnhub",
        LOG_LEVEL: "info",
      },
      secrets: {
        FEED_TOKEN: EcsSecret.fromSecretsManager(props.finnhubSecret),
      },
    });

    //
    // 4️⃣ Security Group
    // Allows HTTPS egress to Finnhub API.
    //
    const sg = new SecurityGroup(this, "FeedSG", { vpc });
    sg.addEgressRule(Peer.anyIpv4(), Port.tcp(443));

    //
    // 5️⃣ Fargate Service
    // A single always-on container that runs 24x7.
    //
    new FargateService(this, "FeedService", {
      cluster,
      taskDefinition: task,
      desiredCount: 1,
      assignPublicIp: true, // gives container direct outbound internet
      securityGroups: [sg],
    });

    //
    // 6️⃣ Permissions
    // The container can send messages to SQS and read its Finnhub secret.
    //
    props.queue.grantSendMessages(task.taskRole);
    props.finnhubSecret.grantRead(task.taskRole);
  }
}
