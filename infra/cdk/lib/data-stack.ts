/**
 * DataStack — 2 of 5.
 *
 * RDS PostgreSQL 16, `db.t4g.micro`, private, encrypted, single-AZ, one-day backup,
 * `RemovalPolicy.DESTROY`.
 *
 * Every one of those is a demo-scoped choice, and the destroy policy is the one that will look like a
 * mistake later. It is not: this database holds a five-day demo's audit trail, and the alternative —
 * a retained snapshot nobody remembers creating — is a cost that outlives the project and a copy of
 * the audit trail outside the account's teardown story. **If the audit trail matters after the demo,
 * export it deliberately** (`verify_audit_chain.py` reads it, and the chain is what makes an export
 * self-validating). Do not rely on a snapshot.
 *
 * What is *not* here: no session table. Session records are live state for the duration of one stream
 * and the durable artifact is the audit trail. Putting live state in Postgres would add a table whose
 * columns then have to be argued past the structural deny-list, for data that is worthless five
 * minutes later (see `gateway/app/session_registry.py`).
 */
import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as rds from 'aws-cdk-lib/aws-rds';
import { Construct } from 'constructs';

export interface DataStackProps extends cdk.StackProps {
  readonly vpc: ec2.IVpc;
  readonly databaseSecurityGroup: ec2.ISecurityGroup;
}

export class DataStack extends cdk.Stack {
  public readonly database: rds.DatabaseInstance;

  constructor(scope: Construct, id: string, props: DataStackProps) {
    super(scope, id, props);

    const parameterGroup = new rds.ParameterGroup(this, 'AuditPg', {
      engine: rds.DatabaseInstanceEngine.postgres({
        version: rds.PostgresEngineVersion.VER_16_6,
      }),
      description: 'SIH26104 audit database parameters',
      parameters: {
        /**
         * Log every statement slower than 500 ms. Safe *because of* what the schema is: the audit
         * table is feature-only and carries no audio, no transcript, and no raw caller reference, so
         * a logged statement cannot leak either (rules.md R-14, R-16). On a schema that held raw
         * values this parameter would be a privacy defect rather than an operational aid.
         */
        log_min_duration_statement: '500',
        // Off. It would log the *values* of DML statements, and even on a feature-only schema that
        // writes the HMAC pseudonym into the log — a pseudonym is still a per-caller identifier.
        log_statement: 'none',
        timezone: 'UTC',
      },
    });

    this.database = new rds.DatabaseInstance(this, 'AuditDb', {
      engine: rds.DatabaseInstanceEngine.postgres({
        version: rds.PostgresEngineVersion.VER_16_6,
      }),
      instanceType: ec2.InstanceType.of(ec2.InstanceClass.BURSTABLE4_GRAVITON, ec2.InstanceSize.MICRO),
      vpc: props.vpc,
      // PRIVATE_ISOLATED — no route to the NAT at all. The database has no internet path even if a
      // security group were wrong.
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
      securityGroups: [props.databaseSecurityGroup],
      /**
       * CDK generates the password into a new Secrets Manager secret and it never appears in the
       * template, in CloudFormation parameters, or in a task definition's `environment` block
       * (rules.md R-34). `SecretsStack` grants read on it; ECS injects it through `secrets:`.
       *
       * Note this is a *different* secret from the Phase-0 placeholder `sih26104/db-password`. The
       * placeholder exists to break the ordering deadlock during initial wiring; this one is the
       * authority once the instance exists. Do not point the app at both.
       */
      credentials: rds.Credentials.fromGeneratedSecret('sih26104', {
        secretName: 'sih26104/rds-generated-credentials',
      }),
      databaseName: 'sih26104',
      allocatedStorage: 20,
      // Off. Autoscaling storage on a demo database converts a runaway insert loop from an error into
      // a bill.
      maxAllocatedStorage: undefined,
      storageType: rds.StorageType.GP3,
      storageEncrypted: true,
      multiAz: false,
      backupRetention: cdk.Duration.days(1),
      deleteAutomatedBackups: true,
      deletionProtection: false,
      // See the file header. This is deliberate, and it is the reason teardown is a single command.
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      parameterGroup,
      // Postgres logs to CloudWatch; retention is set on the log group by ComputeStack's convention
      // of one week for the whole demo.
      cloudwatchLogsExports: ['postgresql'],
      cloudwatchLogsRetention: 7,
      // Off. Both bill, and neither answers a question this demo will ask.
      enablePerformanceInsights: false,
      monitoringInterval: undefined,
      autoMinorVersionUpgrade: false,
      // A minor-version upgrade mid-demo would restart the instance. Pinned window well away from any
      // plausible demo slot, and `autoMinorVersionUpgrade: false` means it should never fire.
      preferredMaintenanceWindow: 'Sun:18:00-Sun:19:00',
      preferredBackupWindow: '17:00-17:30',
    });

    new cdk.CfnOutput(this, 'DbEndpoint', {
      value: this.database.dbInstanceEndpointAddress,
      description: 'Consumed by ComputeStack to build DATABASE_URL',
    });
    new cdk.CfnOutput(this, 'DbGeneratedSecretArn', {
      value: this.database.secret?.secretArn ?? 'NONE',
      description: 'The generated credentials secret. Never echo its value.',
    });
  }
}
