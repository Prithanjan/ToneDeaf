/**
 * ComputeStack — 4 of 5.
 *
 * ECS cluster `sih26104`, the Cloud Map namespace `sih26104.local`, two task definitions, two
 * services, one GPU Auto Scaling Group, and the one-shot `gateway-migrate` task.
 *
 * **Two launch types, on purpose.** The Gateway is Fargate; the Scorer is EC2 on a `g4dn.xlarge`
 * capacity provider. That is not an inconsistency to tidy up later — Fargate has no GPU support at
 * all, so the Scorer cannot run there, and putting the *Gateway* on EC2 to match would mean paying for
 * an instance to host a CPU-only Python process that Fargate runs for nothing at desired count 0.
 * The split is what makes "idle costs approximately zero" true.
 *
 * **`deployRuntime` is the whole cost story.** When it is false — the default — every number that can
 * cost money in this file is 0: both ECS services synth with `desiredCount: 0`, and the GPU ASG synths
 * with min, max, *and* desired all 0. A `cdk deploy` of this stack therefore cannot start GPU spend,
 * which is what makes rules.md R-29 ("no `git push` can start GPU spend") a property of the template
 * rather than a rule someone has to remember. `npm run synth:check-zero` asserts it against the
 * rendered CloudFormation, so the guarantee survives an edit to this file.
 *
 * ⚠️ `deployRuntime: true` is not the end of the session. `stop-runtime.yml` must still be run
 * (rules.md R-30) — the Budget alarm in `CostSafetyStack` is a delayed backstop, not a circuit
 * breaker.
 */
import * as cdk from 'aws-cdk-lib';
import * as autoscaling from 'aws-cdk-lib/aws-autoscaling';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as rds from 'aws-cdk-lib/aws-rds';
import * as sm from 'aws-cdk-lib/aws-secretsmanager';
import { Construct } from 'constructs';

export interface ComputeStackProps extends cdk.StackProps {
  readonly deployRuntime: boolean;
  readonly vpc: ec2.IVpc;
  readonly gatewaySecurityGroup: ec2.ISecurityGroup;
  readonly scorerSecurityGroup: ec2.ISecurityGroup;
  /**
   * The ALB's security group. The load balancer itself lives in this stack rather than in `EdgeStack`
   * — see the note on `gatewayAlb` below for the cycle that forces it.
   */
  readonly albSecurityGroup: ec2.ISecurityGroup;
  readonly database: rds.IDatabaseInstance;
  /**
   * The assembled connection URL, `postgresql://` scheme. See the note on `DATABASE_URL` below for why
   * this is its own secret, and `infra/cdk/lib/secrets-stack.ts:27` for why the scheme matters.
   */
  readonly databaseUrl: sm.ISecret;
  readonly ticketSigningKey: sm.ISecret;
  readonly hmacKey: sm.ISecret;
  readonly auditChainKey: sm.ISecret;
  /** `sha256:…`, never a tag. Empty is tolerated only while `deployRuntime` is false. */
  readonly gatewayImageDigest: string;
  readonly scorerImageDigest: string;
  /** Exact origins, comma-separated. Empty makes the Gateway refuse to boot — deliberately. */
  readonly allowedOrigins: string;
  readonly jwtIssuer: string;
  readonly jwtJwksUrl: string;
  readonly jwtAudience: string;
  readonly gitCommit: string;
}

/** Matches the names `aws-setup-instructions.md` §7.2 and §8 verify against. Changing one breaks a
 *  documented command, so they are constants rather than inline strings. */
const CLUSTER_NAME = 'sih26104';
const NAMESPACE = 'sih26104.local';
const SCORER_DNS_NAME = 'scorer';
const GPU_ASG_NAME = 'scorer-gpu-asg';
const SCORER_PORT = 50051;
const GATEWAY_PORT = 8080;

export class ComputeStack extends cdk.Stack {
  public readonly cluster: ecs.Cluster;
  public readonly gatewayService: ecs.FargateService;
  public readonly scorerService: ecs.Ec2Service;
  /**
   * The internal ALB in front of the Gateway. **`EdgeStack` consumes this; it does not create it.**
   *
   * It reads as belonging to the edge, and the original plan put it there. It cannot go there: an ECS
   * service with a load-balancer configuration gets a hard CloudFormation dependency on the ALB's
   * *listener* (the registration fails if the listener does not exist yet). With the ALB in `EdgeStack`
   * and `EdgeStack` depending on `ComputeStack` for the service, that dependency closes a loop —
   * `cdk synth` reports it as:
   *
   *   «DependencyCycle» 'EdgeStack' depends on 'ComputeStack' … Adding this dependency
   *   ({ComputeStack/GatewayService/Service}.addDependency({EdgeStack/GatewayAlb/HttpListener}))
   *   would create a cyclic reference.
   *
   * So the load-balancing path lives with the thing being balanced, and `EdgeStack` is left as purely
   * the public edge: S3, CloudFront, and the security-group rule that joins them. The stack *count*
   * and *order* in D-14 are unchanged; only the ownership of the ALB moved.
   */
  public readonly gatewayAlb: elbv2.ApplicationLoadBalancer;

  constructor(scope: Construct, id: string, props: ComputeStackProps) {
    super(scope, id, props);

    const { deployRuntime } = props;

    /**
     * Runtime scale, in one place.
     *
     * `desiredCount` and the three ASG capacities are the only knobs that turn a deploy into a bill,
     * so they are derived from a single boolean here rather than repeated at four call sites where one
     * could be missed in a refactor.
     */
    const serviceDesiredCount = deployRuntime ? 1 : 0;
    const gpuCapacity = deployRuntime ? 1 : 0;

    // ── Images: digests, never tags ───────────────────────────────────────────────────────────────

    const gatewayRepo = ecr.Repository.fromRepositoryName(this, 'GatewayRepo', 'sih26104/gateway');
    const scorerRepo = ecr.Repository.fromRepositoryName(this, 'ScorerGpuRepo', 'sih26104/scorer-gpu');

    /**
     * A digest names one immutable set of bytes; a tag names whatever was pushed last. The parity
     * claim this project makes — same model hash, same calibration hash, same policy hash across
     * tiers — is only checkable if the thing that ran is identifiable, and `:latest` is not
     * identifiable (rules.md R-06).
     *
     * The empty-digest case is handled asymmetrically on purpose:
     *
     * - `deployRuntime: false` → a sentinel tag. The stack still synths, so `synth:check-zero` can
     *   run in CI on a clean checkout, and nothing pulls the image because desired count is 0.
     * - `deployRuntime: true` → **throw at synth**. This is the only path that can cost money, and
     *   failing here is far better than failing at task launch, where the symptom is a service
     *   quietly stuck in PENDING while an instance bills.
     */
    const imageFor = (repo: ecr.IRepository, digest: string, which: string): ecs.ContainerImage => {
      if (!digest) {
        if (deployRuntime) {
          throw new Error(
            `${which}ImageDigest is empty but deployRuntime=true. Set it to the sha256 digest ` +
              `pushed by the build workflow (cdk.json / --context ${which}ImageDigest=sha256:…). ` +
              `Deploying the runtime without a pinned digest is how an unreviewed image reaches the ` +
              `GPU tier.`,
          );
        }
        return new ecs.EcrImage(repo, 'NO_IMAGE_DIGEST_CONFIGURED');
      }
      if (!digest.startsWith('sha256:')) {
        throw new Error(
          `${which}ImageDigest must be a digest beginning "sha256:", got ${JSON.stringify(digest)}. ` +
            `A tag is not acceptable here (rules.md R-06).`,
        );
      }
      return new ecs.EcrImage(repo, digest);
    };

    // ── Cluster + service discovery ───────────────────────────────────────────────────────────────

    this.cluster = new ecs.Cluster(this, 'Cluster', {
      clusterName: CLUSTER_NAME,
      vpc: props.vpc,
      // Off. It bills per cluster per hour and answers questions this demo does not ask; the
      // per-service metrics ECS emits for free are enough to see whether a task is running.
      containerInsightsV2: ecs.ContainerInsights.DISABLED,
    });

    /**
     * Private DNS, so the Gateway reaches the Scorer at `scorer.sih26104.local:50051` and no IP is
     * ever written down (architecture.md §4.2). The Scorer's task IP changes on every replacement;
     * a hard-coded address would be a redeploy of the Gateway each time.
     *
     * ⚠️ 10-second TTL, and it is not arbitrary. gRPC channels resolve once and cache, so a long TTL
     * means the Gateway keeps dialling a task that no longer exists after a Scorer replacement, and
     * the symptom is `UNAVAILABLE` on a Scorer that is demonstrably healthy.
     */
    this.cluster.addDefaultCloudMapNamespace({
      name: NAMESPACE,
      type: cdk.aws_servicediscovery.NamespaceType.DNS_PRIVATE,
      useForServiceConnect: false,
    });

    // ── GPU capacity ──────────────────────────────────────────────────────────────────────────────

    const gpuAsg = new autoscaling.AutoScalingGroup(this, 'ScorerGpuAsg', {
      autoScalingGroupName: GPU_ASG_NAME,
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      instanceType: new ec2.InstanceType('g4dn.xlarge'),
      /**
       * The GPU-flavoured ECS-optimised AMI. It ships the NVIDIA driver and the container runtime
       * hook; the standard AMI does not, and on it a task requesting a GPU stays PENDING with no
       * error that names the cause.
       */
      machineImage: ecs.EcsOptimizedImage.amazonLinux2(ecs.AmiHardwareType.GPU),
      securityGroup: props.scorerSecurityGroup,
      // All three zero unless deployRuntime. maxCapacity is zero too: a max above zero would let
      // anything that can call SetDesiredCapacity start an instance.
      minCapacity: gpuCapacity,
      maxCapacity: gpuCapacity,
      /**
       * ⚠️ `cdk synth` warns about this line, and the warning describes the desired behaviour:
       *
       *   «AutoScalingGroup» desiredCapacity has been configured. Be aware this will reset the size
       *   of your AutoScalingGroup on every deployment.
       *
       * The warning exists because a stack that re-pins desired capacity will undo whatever a scaling
       * policy or an operator has since done. That is exactly what is wanted here: with
       * `deployRuntime=false`, **every** deploy drives the GPU fleet back to 0, so a forgotten
       * `SetDesiredCapacity` from a debugging session cannot survive into the next deploy and bill
       * quietly. Suppressing the warning by dropping the property would make the ASG's size ambient
       * state rather than a declared invariant.
       */
      desiredCapacity: gpuCapacity,
      // No SSH key, ever. There is no bastion and no shell into the host (rules.md R-36); debugging
      // is CloudWatch Logs and the Health RPC.
      keyPair: undefined,
      requireImdsv2: true,
    });

    /**
     * Managed scaling **disabled**, and this is a deliberate refusal of the AWS default.
     *
     * With managed scaling on, ECS owns the ASG's desired capacity and adjusts it from task demand.
     * That would make the ASG's numbers a *reading* rather than a *fact* — and those numbers are the
     * cost guardrail this whole stack is built around. `aws autoscaling
     * describe-auto-scaling-groups` showing 0/0/0 (aws-setup-instructions.md §7.2) has to mean the
     * runtime is off, not "the runtime is off at the instant you looked".
     *
     * Managed termination protection is also explicitly false. When it is on, instances are protected
     * from scale-in and the ASG **cannot be scaled to zero by hand** — which would break
     * `stop-runtime.yml`, the one control that must work every session without exception
     * (rules.md R-30). A guardrail that the platform can veto is not a guardrail.
     *
     * The cost of turning both off: with the ASG at 0, a Scorer task sits in PENDING forever instead
     * of summoning an instance. That is the correct failure — visible, free, and self-explanatory.
     */
    const capacityProvider = new ecs.AsgCapacityProvider(this, 'ScorerCapacityProvider', {
      capacityProviderName: 'scorer-gpu',
      autoScalingGroup: gpuAsg,
      enableManagedScaling: false,
      enableManagedTerminationProtection: false,
      // Nothing to drain: one task, no in-flight work worth preserving across a replacement, and a
      // stream that is cut is retried by the client.
      enableManagedDraining: false,
    });
    this.cluster.addAsgCapacityProvider(capacityProvider);

    // ── Logging ───────────────────────────────────────────────────────────────────────────────────

    /**
     * Explicit log groups, named to match the `aws logs tail` commands in the runbook. Created here
     * rather than left to ECS so that retention and removal policy are stated: a log group ECS
     * creates implicitly defaults to *never expire* and survives `cdk destroy`, which is a small
     * permanent cost and a copy of operational data outside the teardown story.
     *
     * One week. Long enough to review a demo day, short enough that it cannot become an archive.
     */
    const logGroup = (name: string) =>
      new logs.LogGroup(this, `${name}LogGroup`, {
        logGroupName: `/ecs/${CLUSTER_NAME}/${name}`,
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });

    // ── Shared task-role posture ──────────────────────────────────────────────────────────────────

    /**
     * A task role with **no policies at all**, shared by both services, and that is a statement
     * rather than an oversight: neither application makes a single AWS API call. Secrets arrive
     * through the *execution* role at container start, logs are written by the log driver (also the
     * execution role), the database is reached with a password, and the Scorer is reached over the
     * network. Nothing in either process needs to sign an AWS request.
     *
     * Recording that as an empty role means a future policy addition has to be argued for, and shows
     * up in a diff as a new statement on a role that had none.
     */
    const taskRole = new iam.Role(this, 'TaskRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      description:
        'Intentionally empty. Neither the Gateway nor the Scorer calls any AWS API; secrets and logs ' +
        'go through the execution role. Adding a policy here needs a reason.',
    });

    // ── Gateway task definition (Fargate) ─────────────────────────────────────────────────────────

    const gatewayTaskDef = new ecs.FargateTaskDefinition(this, 'GatewayTaskDef', {
      family: 'gateway',
      cpu: 1024,
      memoryLimitMiB: 2048,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.X86_64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
      taskRole,
    });

    /**
     * Non-secret configuration. Every key here is a declared field of `gateway/app/config.py`'s
     * `Settings`, and the values are the AWS tier's values — the tier is a *value*, never a code
     * branch (rules.md R-04).
     *
     * ⚠️ A misspelled key is silent. `BaseSettings` ignores environment variables that do not match a
     * field (verified: `extra="forbid"` bites on `.env` file keys, not on `os.environ`, which is why
     * ECS's own injected `AWS_*` and `ECS_CONTAINER_METADATA_URI_V4` do not crash the container). So
     * `SCORER_DEADLNE_MS` would not error — it would leave the default in place. Spell-check against
     * the dataclass, not against intuition.
     */
    const gatewayEnvironment: Record<string, string> = {
      /**
       * `aws-gpu` — one of exactly two members, the other being `local-cpu`. There is deliberately no
       * `aws-cpu`: the CPU tier is a *local* parity tier. Note the consequence, because the config
       * enforces it and it is easy to trip over — under `aws-gpu` the Gateway refuses to start
       * unless the provider is CUDA, every origin is https, and the issuer is not the local test
       * harness (gateway/app/config.py::_validate).
       */
      DEPLOYMENT_PROFILE: 'aws-gpu',
      EXECUTION_PROVIDER: 'CUDAExecutionProvider',

      /**
       * Empty until `EdgeStack` has been deployed once and the CloudFront domain is known — which is
       * a genuine two-pass deploy, not an oversight. `ComputeStack` cannot read the distribution
       * domain, because `EdgeStack` depends on this stack's Gateway service for its target group; the
       * reference would be a cycle.
       *
       * Empty is safe. The Gateway's `_no_wildcard_origins` validator rejects an empty allow-list and
       * **the process refuses to boot** — so the failure mode of forgetting the second pass is a
       * container that will not start, not a Gateway that accepts every origin. Fail-closed by
       * construction rather than by remembering.
       *
       * Second pass: read `DistributionDomainName` from EdgeStack's outputs, set `allowedOrigins` in
       * cdk.json, redeploy ComputeStack.
       */
      ALLOWED_ORIGINS: props.allowedOrigins,

      JWT_ISSUER: props.jwtIssuer,
      JWT_JWKS_URL: props.jwtJwksUrl,
      JWT_AUDIENCE: props.jwtAudience,

      // Cloud Map private DNS. No IP, no ALB in front of the Scorer — the Gateway is the only thing
      // that may reach it (NetworkStack's SG chain enforces the other half of that claim).
      SCORER_TARGET: `${SCORER_DNS_NAME}.${NAMESPACE}:${SCORER_PORT}`,
      // 400 ms on the GPU tier. A deadline, not a timeout to tune upward: a score that arrives after
      // the window it describes has passed is not late, it is wrong, and the policy layer treats a
      // missing window as ineligible rather than waiting for it (R-09).
      SCORER_DEADLINE_MS: '400',
      SCORER_MAX_CONCURRENCY: '4',

      // Baked into the image at an absolute path so this value is identical on both tiers; the
      // Compose bind mount shadows the same path locally (gateway/Dockerfile).
      POLICY_BUNDLE_PATH: '/policy/policy.yaml',
      CALIBRATION_PATH: '/policy/calibration.json',

      AUDIT_RETENTION_DAYS: '7',
      TENANT_ID: 'demo-tenant',
      // Four concurrent streams, then refuse. Never queue audio (rules.md R-20): a queued frame is
      // scored against a window that has already gone past, and the honest answer to "too many
      // callers" is to decline the fifth stream, not to degrade all five.
      MAX_CONCURRENT_STREAMS: '4',
      LOG_LEVEL: 'INFO',
      // Stamped into the parity set and every audit row, so a recorded decision can be tied to the
      // code that made it.
      GIT_COMMIT: props.gitCommit,
    };

    /**
     * Secrets reach the container through `secrets:` — Secrets Manager resolved by the execution role
     * at container start — and **never** through `environment:` (rules.md R-34).
     *
     * The difference is not stylistic. A value in `environment:` is stored in the task definition,
     * returned by `ecs describe-task-definition` to anyone with read access, printed in the `cdk
     * diff` a human approves, and kept in every revision of that task definition forever. `secrets:`
     * stores only an ARN.
     *
     * `DATABASE_URL` is its own secret rather than being assembled here, and that is worth a note
     * because it looks redundant next to `sih26104/db-password`. `Settings.database_url` is a complete
     * connection URL, and ECS `secrets:` injects exactly one Secrets Manager value per variable — it
     * cannot interpolate a password into a URL. The alternatives were to put host and database name
     * in `environment:` and assemble in an entrypoint shim (a new moving part, and the password lands
     * in a process argument list), or to change the app's config contract. A fifth secret holding the
     * assembled URL is the smallest honest option.
     *
     * ⚠️ That secret's value must use the `postgresql://` scheme, **not** `postgresql+asyncpg://`.
     * Nothing in this stack can validate that — the value is opaque here, and a wrong scheme surfaces
     * as a Gateway task that starts and dies in a loop with a `ValueError` from
     * `asyncpg.create_pool()` (`gateway/app/main.py:153`) naming no AWS resource. The `+driver` suffix
     * is SQLAlchemy dialect syntax, and although `gateway/requirements.txt` does pin `sqlalchemy`
     * (alembic needs it; this image also runs the migration), no request path imports it. See
     * `infra/cdk/lib/secrets-stack.ts:27` for the full reasoning and the two other consumers that
     * normalize around the plain form.
     *
     * ⚠️ Drift hazard, stated plainly: `sih26104/db-password` and `sih26104/database-url` contain the
     * same password in two shapes. Rotate them **together**. The password secret is the credential of
     * record for a human running `psql` from a one-shot task; the URL is what the application reads.
     */
    const gatewaySecrets: Record<string, ecs.Secret> = {
      DATABASE_URL: ecs.Secret.fromSecretsManager(props.databaseUrl),
      HMAC_KEY: ecs.Secret.fromSecretsManager(props.hmacKey),
      TICKET_SIGNING_KEY: ecs.Secret.fromSecretsManager(props.ticketSigningKey),
      AUDIT_CHAIN_KEY: ecs.Secret.fromSecretsManager(props.auditChainKey),
    };

    const gatewayContainer = gatewayTaskDef.addContainer('gateway', {
      image: imageFor(gatewayRepo, props.gatewayImageDigest, 'gateway'),
      // A container that exits must take the task down, so ECS replaces it and the failure is visible
      // as a restart rather than as a task that is running with nothing in it.
      essential: true,
      environment: gatewayEnvironment,
      secrets: gatewaySecrets,
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'gateway',
        logGroup: logGroup('gateway'),
        mode: ecs.AwsLogDriverMode.NON_BLOCKING,
      }),
      /**
       * Liveness, matching the Dockerfile: `/healthz`, not `/readyz`. `/readyz` additionally checks
       * the database and the Scorer's execution provider, so using it here would let a Scorer outage
       * kill a Gateway that is working correctly and correctly reporting that its dependency is down.
       */
      healthCheck: {
        command: [
          'CMD-SHELL',
          `python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:${GATEWAY_PORT}/healthz',timeout=2).status==200 else 1)"`,
        ],
        interval: cdk.Duration.seconds(15),
        timeout: cdk.Duration.seconds(3),
        retries: 3,
        startPeriod: cdk.Duration.seconds(30),
      },
      // The uvicorn command (`--workers 1`, `--ws-max-size 65536`) is the image's CMD and is
      // deliberately NOT overridden here. `--workers 1` is a correctness constraint, not tuning: the
      // session registry, the ticket replay cache, and the audit chain head are all in-process, and a
      // second worker would fork the hash chain into something a verifier reports as tampering.
    });
    gatewayContainer.addPortMappings({
      containerPort: GATEWAY_PORT,
      protocol: ecs.Protocol.TCP,
    });

    // ── Scorer task definition (EC2 + GPU) ────────────────────────────────────────────────────────

    const scorerTaskDef = new ecs.Ec2TaskDefinition(this, 'ScorerTaskDef', {
      family: 'scorer',
      // awsvpc, so the Scorer gets its own ENI and its own security group. With bridge networking the
      // Scorer would inherit the host's, and "the Scorer's only ingress is the Gateway SG" — the
      // network half of the detection/decision separation — would stop being expressible.
      networkMode: ecs.NetworkMode.AWS_VPC,
      taskRole,
    });

    const scorerContainer = scorerTaskDef.addContainer('scorer', {
      image: imageFor(scorerRepo, props.scorerImageDigest, 'scorer'),
      essential: true,
      /**
       * `g4dn.xlarge` is 4 vCPU / 16 GiB / 1 T4. Reserving 3 vCPU and 12 GiB leaves room for the ECS
       * agent and the NVIDIA runtime; requesting the full instance is how a task becomes unplaceable
       * on the instance provisioned for it.
       */
      cpu: 3072,
      memoryLimitMiB: 12288,
      gpuCount: 1,
      environment: {
        DEPLOYMENT_PROFILE: 'aws-gpu',
        // Asserted, not hoped for. Under `aws-gpu` a CPU provider is a hard startup failure, because
        // a silent CUDA→CPU fallback invalidates every latency number recorded that day and — worse —
        // still produces scores (rules.md R-45).
        EXECUTION_PROVIDER: 'CUDAExecutionProvider',
        /**
         * Still the mock. The long name is the point: it travels in every gRPC response and every
         * audit row, so a mock score cannot be quoted as a detection result without the label
         * attached. Promote this only when a trained ONNX artifact and a fitted calibration exist
         * with a release-manifest entry.
         */
        DETECTOR_MODE: 'MOCK_SMOKE_MODE_NOT_A_DETECTOR',
        // One of research_only | demo_eligible | policy_eligible. `research_only` is the only honest
        // value without a trained artifact, and promoting this string is a claim about evidence.
        ARTIFACT_STATE: 'research_only',
        CALIBRATION_PATH: '/policy/calibration.json',
        MODEL_PATH: '/models/aasist.onnx',
        GRPC_PORT: String(SCORER_PORT),
        // One stream at 2.56 s windows on a 640 ms hop is ~1.6 scores/s. These are headroom for a
        // demo, not a throughput claim — the p95 sweep (H-4) is what would justify a number.
        GRPC_MAX_WORKERS: '4',
        ORT_INTRA_OP_THREADS: '2',
        LOG_LEVEL: 'INFO',
        GIT_COMMIT: props.gitCommit,
      },
      // No secrets. The Scorer holds none, and that is structural: it never sees a caller reference,
      // never writes an audit row, and never touches the database, so there is nothing for it to
      // authenticate to (architecture.md §4.2).
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'scorer',
        logGroup: logGroup('scorer'),
        mode: ecs.AwsLogDriverMode.NON_BLOCKING,
      }),
      /**
       * The application's own entrypoint, not a port check. A TCP probe passes while the model is
       * unloaded or the servicer raises on every call.
       *
       * ⚠️ It asserts `ready` only — not `execution_provider`, not `contract_vector_parity_ok`, both
       * of which `HealthResponse` carries. On this tier that gap matters most: a Scorer that fell back
       * to CPU comes up *healthy* and emits numbers. Tracked against `scorer/app/server.py:528`.
       */
      healthCheck: {
        command: ['CMD', 'python', '-m', 'app.server', '--healthcheck'],
        interval: cdk.Duration.seconds(15),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(60),
      },
    });
    scorerContainer.addPortMappings({
      containerPort: SCORER_PORT,
      protocol: ecs.Protocol.TCP,
    });

    // ── Services ──────────────────────────────────────────────────────────────────────────────────

    this.gatewayService = new ecs.FargateService(this, 'GatewayService', {
      serviceName: 'gateway',
      cluster: this.cluster,
      taskDefinition: gatewayTaskDef,
      desiredCount: serviceDesiredCount,
      // No public IP. Egress is via the shared NAT; ingress is only from the ALB SG.
      assignPublicIp: false,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [props.gatewaySecurityGroup],
      /**
       * `minHealthyPercent: 0` because the service runs exactly one task. At 100 ECS would need to
       * start a second task before stopping the first, and with in-process session state and a
       * single-writer audit chain, two Gateways alive at once is precisely what must not happen. The
       * cost is a few seconds of downtime on deploy, which for a demo is not a cost.
       */
      minHealthyPercent: 0,
      maxHealthyPercent: 100,
      // Roll back automatically on a failed deploy, so a bad image digest leaves the previous task
      // definition running instead of an empty service.
      circuitBreaker: { rollback: true },
      /**
       * Off. ECS Exec is a shell into a running container — the same capability the plan refused when
       * it ruled out a bastion and SSH (rules.md R-36). Turning it on for convenience would reintroduce
       * an interactive path to a process that holds the audit chain key in memory.
       */
      enableExecuteCommand: false,
    });

    // ── The Gateway's load balancer ────────────────────────────────────────────────────────────────

    /**
     * Internal. There is no public path to this load balancer at all — its only ingress is the security
     * group CloudFront creates for its VPC origin, and that rule is added in `EdgeStack` because the
     * group does not exist until the distribution does.
     *
     * The alternative shape, an internet-facing ALB plus a "did the request carry CloudFront's secret
     * header" check, leaves a public endpoint that works for anyone who finds it and guesses the header.
     */
    this.gatewayAlb = new elbv2.ApplicationLoadBalancer(this, 'GatewayAlb', {
      vpc: props.vpc,
      internetFacing: false,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroup: props.albSecurityGroup,
      /**
       * 300 s. This is the WebSocket lifetime cap, not a tuning knob — the ALB closes a connection at
       * this timeout regardless of what the application thinks. A stream where the caller is *silent*
       * still sends frames, so it is not idle and is not cut; a stream that stalls upstream is.
       */
      idleTimeout: cdk.Duration.seconds(300),
      http2Enabled: true,
    });

    const gatewayTargetGroup = new elbv2.ApplicationTargetGroup(this, 'GatewayTargetGroup', {
      vpc: props.vpc,
      port: GATEWAY_PORT,
      protocol: elbv2.ApplicationProtocol.HTTP,
      // IP targets, because the Gateway runs on Fargate with awsvpc networking.
      targetType: elbv2.TargetType.IP,
      /**
       * `/healthz`, deliberately **not** `/readyz`.
       *
       * `/readyz` checks the database and the Scorer's execution provider. Using it here would make a
       * Scorer outage deregister a *healthy* Gateway — so the one component that could still serve
       * `/v1/sessions` and report the dependency failure to a client would itself become unreachable.
       * The ALB should remove a Gateway that is broken, not one whose dependency is.
       */
      healthCheck: {
        path: '/healthz',
        healthyHttpCodes: '200',
        interval: cdk.Duration.seconds(15),
        timeout: cdk.Duration.seconds(5),
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 3,
      },
      /**
       * 5 s. The default 300 s keeps sending connections to a replaced task for five minutes. There is
       * nothing to drain gracefully here — in-flight streams are cut either way and the client retries
       * — so a long window buys nothing and delays every deploy.
       */
      deregistrationDelay: cdk.Duration.seconds(5),
      // Stickiness left off (the default): it would imply more than one Gateway task, and there is
      // exactly one by design, because the audit chain is single-writer.
    });

    /**
     * Plain HTTP, and that is correct here rather than a lapse.
     *
     * TLS terminates at CloudFront. This ALB is internal, in a private subnet, reachable only from
     * CloudFront's VPC-origin security group, and the hop between them stays inside the AWS network. A
     * second TLS termination would need a certificate for a private name that nothing validates. What
     * matters is that no plaintext hop exists outside the VPC — and none does.
     */
    this.gatewayAlb.addListener('HttpListener', {
      port: 80,
      protocol: elbv2.ApplicationProtocol.HTTP,
      defaultTargetGroups: [gatewayTargetGroup],
    });

    this.gatewayService.attachToApplicationTargetGroup(gatewayTargetGroup);

    this.scorerService = new ecs.Ec2Service(this, 'ScorerService', {
      serviceName: 'scorer',
      cluster: this.cluster,
      taskDefinition: scorerTaskDef,
      desiredCount: serviceDesiredCount,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [props.scorerSecurityGroup],
      capacityProviderStrategies: [{ capacityProvider: capacityProvider.capacityProviderName, weight: 1 }],
      minHealthyPercent: 0,
      maxHealthyPercent: 100,
      circuitBreaker: { rollback: true },
      enableExecuteCommand: false,
      /**
       * Registers `scorer.sih26104.local`. This is the only way the Gateway learns where the Scorer
       * is; see the TTL note on the namespace above.
       */
      cloudMapOptions: {
        name: SCORER_DNS_NAME,
        dnsRecordType: cdk.aws_servicediscovery.DnsRecordType.A,
        dnsTtl: cdk.Duration.seconds(10),
      },
    });

    // ── One-shot migration task ───────────────────────────────────────────────────────────────────

    /**
     * `gateway-migrate` — the same image as the Gateway with a different command, run with
     * `ecs run-task` (aws-setup-instructions.md §8). Not an entrypoint hook and not a service: a
     * migration that runs on every container start races itself when a task is replaced, and a
     * migration that fails inside the Gateway's startup fails *quietly* as a crash-loop instead of
     * loudly as a task that exited non-zero.
     *
     * **Fargate, matching the Gateway.** The runbook currently says `--launch-type EC2` for this task;
     * that is wrong and is corrected in the doc — EC2 launch type would place it on the GPU ASG, so
     * running a schema migration would require a `g4dn.xlarge` to be up, and with the ASG at 0/0/0 it
     * would sit in PENDING forever. Migrating the database must not cost GPU time.
     *
     * It needs the same secrets (it connects as the application user) but none of the tier or policy
     * configuration, because Alembic reads only `DATABASE_URL`.
     */
    const migrateTaskDef = new ecs.FargateTaskDefinition(this, 'MigrateTaskDef', {
      family: 'gateway-migrate',
      cpu: 512,
      memoryLimitMiB: 1024,
      taskRole,
    });

    migrateTaskDef.addContainer('migrate', {
      image: imageFor(gatewayRepo, props.gatewayImageDigest, 'gateway'),
      essential: true,
      // Overridable per `run-task`, which is how the runbook also uses this definition to verify the
      // structural deny-list against the real RDS schema rather than local Postgres.
      command: ['alembic', '-c', '/app/audit/migrations/alembic.ini', 'upgrade', 'head'],
      environment: {
        // Alembic reads DATABASE_URL and nothing else, but Settings is imported transitively by the
        // migration env, so the tier fields have to be present and valid.
        DEPLOYMENT_PROFILE: 'aws-gpu',
        EXECUTION_PROVIDER: 'CUDAExecutionProvider',
        ALLOWED_ORIGINS: props.allowedOrigins,
        JWT_ISSUER: props.jwtIssuer,
        JWT_JWKS_URL: props.jwtJwksUrl,
        JWT_AUDIENCE: props.jwtAudience,
        SCORER_TARGET: `${SCORER_DNS_NAME}.${NAMESPACE}:${SCORER_PORT}`,
        POLICY_BUNDLE_PATH: '/policy/policy.yaml',
        CALIBRATION_PATH: '/policy/calibration.json',
        TENANT_ID: 'demo-tenant',
        LOG_LEVEL: 'INFO',
        GIT_COMMIT: props.gitCommit,
      },
      secrets: gatewaySecrets,
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'migrate',
        logGroup: logGroup('gateway-migrate'),
        mode: ecs.AwsLogDriverMode.NON_BLOCKING,
      }),
    });

    /**
     * **No database ingress rule here.** `NetworkStack` already grants Gateway SG → 5432 on the
     * database security group (`network-stack.ts:154`), and adding
     * `props.database.connections.allowDefaultPortFrom(...)` here as well is not merely redundant — it
     * is a dependency cycle:
     *
     *   «DependencyCycle» 'DataStack' depends on 'NetworkStack' … Adding this dependency
     *   (NetworkStack -> DataStack/AuditDb/Resource.Endpoint.Port) would create a cyclic reference.
     *
     * `allowDefaultPortFrom` attaches the rule to the *database's* security group — which lives in
     * NetworkStack — and resolves the port from the DB instance's endpoint, which lives in DataStack. So
     * NetworkStack ends up referencing DataStack while DataStack already references NetworkStack.
     *
     * Writing the port as the literal `5432` in NetworkStack is what breaks that loop. It is a
     * duplicated constant, and that is the cheaper of the two costs.
     */

    // ── Outputs ───────────────────────────────────────────────────────────────────────────────────

    new cdk.CfnOutput(this, 'ClusterName', { value: this.cluster.clusterName });
    new cdk.CfnOutput(this, 'GatewayAlbDnsName', {
      value: this.gatewayAlb.loadBalancerDnsName,
      description: 'Internal only — not resolvable or reachable from outside the VPC',
    });
    new cdk.CfnOutput(this, 'ScorerEndpoint', {
      value: `${SCORER_DNS_NAME}.${NAMESPACE}:${SCORER_PORT}`,
      description: 'What SCORER_TARGET is set to. Reachable only from the Gateway SG.',
    });
    new cdk.CfnOutput(this, 'RuntimeState', {
      value: deployRuntime ? 'RUNTIME ON — desiredCount 1, GPU ASG 1' : 'runtime off — all zero',
      description: 'Cross-check against aws-setup-instructions.md §7.2 after every deploy',
    });

    if (!props.allowedOrigins) {
      cdk.Annotations.of(this).addWarning(
        'allowedOrigins is empty, so the Gateway will refuse to start (fail-closed, by design). ' +
          'Deploy EdgeStack, read DistributionDomainName from its outputs, set allowedOrigins in ' +
          'cdk.json, then redeploy ComputeStack. This is the documented two-pass deploy.',
      );
    }
  }
}
