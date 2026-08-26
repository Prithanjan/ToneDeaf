/**
 * SecretsStack — 3 of 5.
 *
 * **This stack creates no secrets.** It references the four Phase-0 Secrets Manager entries by name
 * and re-exports them as typed `ISecret` handles for `ComputeStack` to bind into task definitions.
 *
 * That is the whole design, and the reason is an ordering deadlock: a task definition needs a secret
 * ARN, and an ARN needs the secret to exist. Creating the secrets in CDK would work for the first
 * deploy and then make `cdk destroy` delete them — taking `sih26104/audit-chain-key` with it, which
 * **irrecoverably invalidates every audit event ever written** (rules.md R-58). The chain is a keyed
 * HMAC; there is no migration path and no recovery.
 *
 * So the secrets are created once, by hand, in Phase 0 (aws-setup-instructions.md §6), and they
 * outlive every stack. `cdk destroy` cannot reach them. That asymmetry is the point.
 *
 * ⚠️ The Phase-0 values are deliberately obvious placeholders (`CHANGE_ME_…`). **Rotate all four to
 * real generated values before any real demo session — and rotate `audit-chain-key` exactly once,
 * before the first session, then never again.**
 */
import * as cdk from 'aws-cdk-lib';
import * as sm from 'aws-cdk-lib/aws-secretsmanager';
import { Construct } from 'constructs';

/** Phase-0 secret names. These strings are a contract with `aws-setup-instructions.md` §6. */
const SECRET_NAMES = {
  dbPassword: 'sih26104/db-password',
  /**
   * The assembled connection URL, e.g. `postgresql://sih26104:<pw>@<endpoint>:5432/sih26104`.
   *
   * ⚠️ The scheme is `postgresql://`, NOT `postgresql+asyncpg://`. The `+driver` suffix is SQLAlchemy
   * dialect syntax, and the Gateway's serving path does not go through SQLAlchemy — it calls
   * `asyncpg.create_pool()` directly (`gateway/app/main.py:153`), and asyncpg validates the scheme
   * itself, raising `ValueError` on anything but `postgresql` / `postgres`. Putting the `+asyncpg`
   * form in this secret is therefore a crash-loop with a CloudWatch `ValueError` that names no AWS
   * resource.
   *
   * The reason this looks wrong: `gateway/requirements.txt:20` really does pin `sqlalchemy==2.0.36`,
   * so grepping the dependency list "confirms" the dialect form. It is there because `alembic==1.14.0`
   * requires it and the Gateway image doubles as the migration image — not because any request path
   * imports it. Nothing under `gateway/app/` does; `main.py:224` even reads the `alembic_version`
   * table with raw SQL through asyncpg rather than reflecting it.
   *
   * The plain form is the only value all three consumers accept: alembic normalizes it upward
   * (`audit/migrations/env.py:57`) and the retention worker strips the suffix downward
   * (`audit/retention_worker.py:559`).
   *
   * A fifth secret, holding the same password as `db-password` in a different shape, because ECS
   * `secrets:` injects exactly one Secrets Manager value per environment variable and cannot
   * interpolate a password into a URL — while `Settings.database_url` is a complete URL. The
   * alternatives were an entrypoint shim that assembles it (putting the password in a process
   * argument list) or changing the application's config contract.
   *
   * ⚠️ Rotate this **together with** `db-password`. Two shapes of one credential is a drift hazard,
   * and the failure is a Gateway that cannot connect while the password secret looks correct.
   */
  databaseUrl: 'sih26104/database-url',
  ticketSigningKey: 'sih26104/ticket-signing-key',
  hmacKey: 'sih26104/hmac-key',
  auditChainKey: 'sih26104/audit-chain-key',
} as const;

export class SecretsStack extends cdk.Stack {
  public readonly databaseSecret: sm.ISecret;
  public readonly databaseUrl: sm.ISecret;
  public readonly ticketSigningKey: sm.ISecret;
  public readonly hmacKey: sm.ISecret;
  public readonly auditChainKey: sm.ISecret;

  constructor(scope: Construct, id: string, props: cdk.StackProps) {
    super(scope, id, props);

    /**
     * `fromSecretNameV2`, not `fromSecretCompleteArn`.
     *
     * Secrets Manager appends a random six-character suffix to every ARN, so the full ARN is not
     * knowable until after creation and cannot be written into a committed template. The name is
     * stable. The cost is that the resulting IAM grant is on `…:secret:<name>-??????`, which is a
     * wildcard — acceptable here because the wildcard is six characters wide on an exact name, not a
     * prefix match on a namespace.
     */
    this.databaseSecret = sm.Secret.fromSecretNameV2(this, 'DbPassword', SECRET_NAMES.dbPassword);
    this.databaseUrl = sm.Secret.fromSecretNameV2(this, 'DatabaseUrl', SECRET_NAMES.databaseUrl);
    this.ticketSigningKey = sm.Secret.fromSecretNameV2(
      this,
      'TicketSigningKey',
      SECRET_NAMES.ticketSigningKey,
    );
    this.hmacKey = sm.Secret.fromSecretNameV2(this, 'HmacKey', SECRET_NAMES.hmacKey);
    this.auditChainKey = sm.Secret.fromSecretNameV2(this, 'AuditChainKey', SECRET_NAMES.auditChainKey);

    /**
     * Outputs are ARNs. Never values.
     *
     * CloudFormation outputs are readable by anyone with `cloudformation:DescribeStacks`, they are
     * retained in stack history, and they are printed by `cdk deploy` into CI logs. An ARN there is
     * inert; a `secretValue` there is a published secret with a permanent record
     * (rules.md R-34).
     */
    for (const [key, secret] of Object.entries({
      DbPasswordArn: this.databaseSecret,
      DatabaseUrlArn: this.databaseUrl,
      TicketSigningKeyArn: this.ticketSigningKey,
      HmacKeyArn: this.hmacKey,
      AuditChainKeyArn: this.auditChainKey,
    })) {
      new cdk.CfnOutput(this, key, { value: secret.secretArn });
    }

    cdk.Annotations.of(this).addInfo(
      'SecretsStack creates nothing. If a secret is missing, create it per aws-setup-instructions.md ' +
        '§6 — do not add it here, or cdk destroy will delete the audit chain key (rules.md R-58).',
    );
  }
}
