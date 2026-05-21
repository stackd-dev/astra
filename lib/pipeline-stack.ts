import * as path from "path";
import { Stack, StackProps, Duration } from "aws-cdk-lib";
import { Construct } from "constructs";
import { Function, Runtime, Code } from "aws-cdk-lib/aws-lambda";
import { Rule, Schedule } from "aws-cdk-lib/aws-events";
import { LambdaFunction } from "aws-cdk-lib/aws-events-targets";
import { Table } from "aws-cdk-lib/aws-dynamodb";
import { Secret } from "aws-cdk-lib/aws-secretsmanager";

export interface PipelineStackProps extends StackProps {
  readonly earningsCalendarTable: Table;
  readonly dataFeedCredentialsSecret: Secret;
}

/**
 * PipelineStack — the stateless research-and-decision brain. Consumes
 * CoreStack's resources; no money-at-risk code. See CLAUDE.md §5 and
 * docs/earnings-system-design.md §3.
 *
 * Built incrementally; current components:
 *   - EarningsFetcher (nightly Finnhub earnings calendar → earnings_calendar)
 */
export class PipelineStack extends Stack {
  constructor(scope: Construct, id: string, props: PipelineStackProps) {
    super(scope, id, props);

    const earningsFetcher = new Function(this, "EarningsFetcher", {
      runtime: Runtime.PYTHON_3_12,
      handler: "handler.handler",
      code: Code.fromAsset(
        path.join(__dirname, "..", "lambdas", "earnings_fetcher"),
      ),
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
  }
}
