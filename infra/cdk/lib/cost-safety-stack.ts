/**
 * CostSafetyStack — **standalone. Not part of the five-stack chain.**
 *
 * AWS Budget → SNS → `RuntimeStopper` Lambda, which zeroes both ECS services and the GPU Auto Scaling
 * Group.
 *
 * **Deploy this immediately after `DataStack`** and before anyone can flip `deployRuntime=true`
 * (aws-setup-instructions.md §7, memory.md D-14/H-5). It references nothing from the other stacks and
 * nothing references it, so its position is an operational rule rather than a graph edge — see the
 * header of `bin/app.ts` for why that is deliberate.
 *
 * ⚠️ **This is a delayed backstop, not a circuit breaker.** AWS Budgets evaluates on a lag measured in
 * hours. By the time this Lambda runs, the money is already spent. It exists to bound a runaway that
 * nobody noticed — it does not make GPU spend safe. The controls that actually work are earlier and
 * cheaper:
 *
 *   1. `deployRuntime=false` by default, so no deploy can start GPU spend at all (rules.md R-29).
 *   2. `stop-runtime.yml` at the end of **every** session without exception (rules.md R-30).
 *
 * If you find yourself relying on this stack, the process has already failed.
 *
 * **Region: us-east-1, not ap-south-1.** AWS Budgets is a global service that operates out of
 * us-east-1, and this is the documented exception to the single-region rule (cdk.json `// region`).
 * Deploying the whole stack there is unconditionally safe: it is required if Budgets can only publish
 * to a us-east-1 topic, and harmless if it can publish anywhere. The Lambda therefore acts
 * *cross-region* — it runs in us-east-1 and calls ECS and Auto Scaling in ap-south-1, which is why
 * `TARGET_REGION` is passed explicitly rather than inferred.
 */
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import * as budgets from 'aws-cdk-lib/aws-budgets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as subscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import { Construct } from 'constructs';

export interface CostSafetyStackProps extends cdk.StackProps {
  readonly monthlyBudgetUsd: number;
  readonly alertEmail: string;
  /** Where the runtime actually lives. `ap-south-1` — see the region note in the file header. */
  readonly targetRegion: string;
}

/**
 * Must match `compute-stack.ts` exactly. A typo here is a Lambda that reports success having stopped
 * nothing, so these are asserted against the real service names in CI
 * (`npm run synth:check-zero` → `scripts/verify_cost_safety.sh`).
 */
const CLUSTER_NAME = 'sih26104';
const SERVICE_NAMES = ['gateway', 'scorer'];
const GPU_ASG_NAME = 'scorer-gpu-asg';

const PLACEHOLDER_EMAIL = 'CHANGE_ME@example.invalid';

export class CostSafetyStack extends cdk.Stack {
  public readonly topic: sns.Topic;
  public readonly stopper: lambda.Function;

  /** Held so `notification()` and the subscription decision read the same value. */
  private readonly alertEmail: string;

  constructor(scope: Construct, id: string, props: CostSafetyStackProps) {
    super(scope, id, props);

    const account = cdk.Stack.of(this).account;
    const targetRegion = props.targetRegion;
    this.alertEmail = props.alertEmail || PLACEHOLDER_EMAIL;

    // ── The notification channel ──────────────────────────────────────────────────────────────────

    this.topic = new sns.Topic(this, 'BudgetAlarmTopic', {
      topicName: 'sih26104-budget-alarm',
      displayName: 'SIH26104 budget alarm',
    });

    /**
     * **Budgets cannot publish to a topic without this.** CDK does not add it, the budget is created
     * happily without it, and the failure is entirely silent: the threshold is crossed, Budgets
     * attempts `SNS:Publish`, gets AccessDenied, and nobody is told anything. A guardrail whose only
     * failure mode is silence is the worst kind.
     *
     * Scoped by `aws:SourceAccount` so another account's budget cannot publish into this topic and
     * trigger a stop here.
     */
    this.topic.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'AllowBudgetsToPublish',
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal('budgets.amazonaws.com')],
        actions: ['SNS:Publish'],
        resources: [this.topic.topicArn],
        conditions: { StringEquals: { 'aws:SourceAccount': account } },
      }),
    );

    /**
     * Email subscription, only when a real address is configured.
     *
     * Subscribing `CHANGE_ME@example.invalid` would create a subscription that can never be confirmed
     * and bounces every send, which reads in the console like a working alert. The warning below plus
     * the `SubscribeCommand` output is the honest alternative: the gap is visible and fixable without
     * a redeploy.
     *
     * Either way, **an SNS email subscription requires the recipient to click a confirmation link.**
     * An unconfirmed subscription delivers nothing.
     */
    if (props.alertEmail && props.alertEmail !== PLACEHOLDER_EMAIL) {
      this.topic.addSubscription(new subscriptions.EmailSubscription(this.alertEmail));
    } else {
      cdk.Annotations.of(this).addWarning(
        `budgetAlertEmail is still ${PLACEHOLDER_EMAIL} — no email subscription was created. The ` +
          'RuntimeStopper Lambda is then the ONLY notification path, and it is a delayed one. Set ' +
          'budgetAlertEmail in cdk.json, or run the SubscribeCommand output of this stack.',
      );
    }

    // ── The automated stop ────────────────────────────────────────────────────────────────────────

    /**
     * Explicit log group. An implicit Lambda log group never expires and survives `cdk destroy`, so
     * the logs of a torn-down stack keep costing money in a region nobody looks at.
     *
     * One month rather than one week, unlike the ECS groups: if this function ever fires, its log is
     * the record of why a session cost money, and that is worth having a month later during a review.
     */
    const stopperLogs = new logs.LogGroup(this, 'RuntimeStopperLogs', {
      logGroupName: '/aws/lambda/sih26104-runtime-stopper',
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.stopper = new lambda.Function(this, 'RuntimeStopper', {
      functionName: 'sih26104-runtime-stopper',
      description:
        'Zeroes both ECS service desired counts AND the GPU ASG min/max/desired. Safe to invoke by hand.',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      // A plain directory asset: zipped as-is, no Docker, no build step. boto3 is in the managed
      // runtime, so there is nothing to install and no dependency to pin.
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda', 'runtime-stopper')),
      /**
       * 60 s. Three API calls plus bounded retries. Long enough to survive a throttle, short enough
       * that a hung call fails fast and shows up on the Errors metric instead of silently timing out
       * at the 15-minute default.
       */
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      logGroup: stopperLogs,
      environment: {
        TARGET_REGION: targetRegion,
        CLUSTER_NAME,
        SERVICE_NAMES: SERVICE_NAMES.join(','),
        ASG_NAME: GPU_ASG_NAME,
      },
      // Left at the account default on purpose. Reserving concurrency of 1 would throttle a
      // concurrent hand invocation during a budget event — and both callers are trying to stop spend.
      // The handler is idempotent, so concurrent runs are harmless.
    });

    /**
     * IAM, scoped to the exact three things the handler touches.
     *
     * Note the split: `autoscaling:DescribeAutoScalingGroups` does not support resource-level
     * permissions and must be granted on `*`, while `UpdateAutoScalingGroup` is scoped by name. The
     * ASG ARN contains a generated UUID segment, so the wildcard sits there and the *name* is exact —
     * this policy cannot touch a differently-named group.
     */
    this.stopper.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'StopEcsServices',
        actions: ['ecs:UpdateService', 'ecs:DescribeServices'],
        resources: SERVICE_NAMES.map(
          (name) => `arn:aws:ecs:${targetRegion}:${account}:service/${CLUSTER_NAME}/${name}`,
        ),
      }),
    );
    this.stopper.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'StopGpuCapacity',
        actions: ['autoscaling:UpdateAutoScalingGroup'],
        resources: [
          `arn:aws:autoscaling:${targetRegion}:${account}:autoScalingGroup:*:autoScalingGroupName/${GPU_ASG_NAME}`,
        ],
      }),
    );
    this.stopper.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'DescribeAsgRequiresWildcard',
        actions: ['autoscaling:DescribeAutoScalingGroups'],
        resources: ['*'],
      }),
    );

    this.topic.addSubscription(new subscriptions.LambdaSubscription(this.stopper));

    // ── The budget ────────────────────────────────────────────────────────────────────────────────

    /**
     * **No cost filter. The budget covers the whole account.**
     *
     * The obvious alternative — filter on `user:Project$sih26104`, which every resource is tagged with
     * — requires the cost allocation tag to be *activated* in the Billing console, a manual step that
     * takes up to 24 hours to take effect and applies only going forward. An unactivated tag filter
     * does not error: the budget reports **$0 spend forever** and never fires. That is a guardrail
     * that looks armed and is not.
     *
     * A whole-account budget cannot have that failure mode. The account is dedicated to this project,
     * so the only cost of the broader scope is that unrelated spend would also trip it — which, in a
     * dedicated account, is information rather than noise.
     *
     * The tags remain useful for cost *attribution* in Cost Explorer and for finding orphans during
     * teardown; they are just not load-bearing for the alarm.
     */
    new budgets.CfnBudget(this, 'MonthlyBudget', {
      budget: {
        budgetName: 'sih26104-monthly',
        budgetType: 'COST',
        timeUnit: 'MONTHLY',
        budgetLimit: { amount: props.monthlyBudgetUsd, unit: 'USD' },
        costTypes: {
          // Credits excluded from the measured amount. With credits *included*, spend covered by the
          // SIH credit grant reads as $0 and the budget never fires until the credits run out — which
          // is precisely the moment it is too late to be told. Measuring gross cost means the
          // threshold tracks consumption of the grant itself.
          includeCredit: false,
          includeRefund: false,
          includeSubscription: true,
          includeTax: true,
          includeUpfront: true,
          includeRecurring: true,
          includeOtherSubscription: true,
          includeSupport: true,
          includeDiscount: true,
          useAmortized: false,
          useBlended: false,
        },
      },
      /**
       * Four thresholds, and only two of them can stop anything.
       *
       * 50 % actual → email. The first honest signal that consumption is real.
       * 80 % actual → email **and stop**. This is the one that matters: stopping at 80 % leaves
       *   headroom to finish a session deliberately, while stopping at 100 % means the budget is
       *   already gone by the time anything reacts.
       * 100 % actual → email and stop. A second attempt, in case the 80 % invocation failed.
       * 100 % forecasted → email only. A forecast is a projection and can be wrong; wiring a *stop* to
       *   a projection would kill live demos on a bad extrapolation. A human decides on this one.
       */
      notificationsWithSubscribers: [
        this.notification('ACTUAL', 50, false),
        this.notification('ACTUAL', 80, true),
        this.notification('ACTUAL', 100, true),
        this.notification('FORECASTED', 100, false),
      ],
    });

    // ── Outputs ───────────────────────────────────────────────────────────────────────────────────

    new cdk.CfnOutput(this, 'ManualStopCommand', {
      description: 'Emergency stop. Does exactly what the budget alarm does, immediately.',
      value: `aws lambda invoke --region ${this.region} --function-name ${this.stopper.functionName} /dev/stdout`,
    });
    new cdk.CfnOutput(this, 'SubscribeCommand', {
      description: 'Add or change the alert email without a redeploy (then confirm the emailed link)',
      value: `aws sns subscribe --region ${this.region} --topic-arn ${this.topic.topicArn} --protocol email --notification-endpoint <YOUR_EMAIL>`,
    });
    new cdk.CfnOutput(this, 'VerifySubscriptionsCommand', {
      description: 'PendingConfirmation here means no email will ever arrive',
      value: `aws sns list-subscriptions-by-topic --region ${this.region} --topic-arn ${this.topic.topicArn} --query 'Subscriptions[].[Protocol,Endpoint,SubscriptionArn]' --output table`,
    });
    new cdk.CfnOutput(this, 'BudgetAlarmTopicArn', { value: this.topic.topicArn });
    new cdk.CfnOutput(this, 'TargetRegion', {
      value: targetRegion,
      description: 'Region the Lambda acts on — this stack itself lives in us-east-1',
    });
  }

  /**
   * One Budgets notification plus its subscriber list.
   *
   * `comparisonOperator: 'GREATER_THAN'` and `thresholdType: 'PERCENTAGE'` are the only combination
   * used, so the threshold numbers read as percentages of `monthlyBudgetUsd` everywhere.
   */
  private notification(
    notificationType: 'ACTUAL' | 'FORECASTED',
    threshold: number,
    alsoStop: boolean,
  ): budgets.CfnBudget.NotificationWithSubscribersProperty {
    return {
      notification: {
        notificationType,
        comparisonOperator: 'GREATER_THAN',
        threshold,
        thresholdType: 'PERCENTAGE',
      },
      /**
       * `alsoStop` picks the subscriber *channel*, and that is the only mechanism available.
       *
       * There is exactly one SNS topic and the Lambda is subscribed to it, so **any** threshold that
       * publishes to the topic triggers a stop. A "warn but do not stop" threshold therefore cannot
       * use the topic at all — it uses Budgets' native EMAIL subscriber, which delivers without going
       * through SNS. The consequence worth knowing: the 50 % and forecast alerts arrive from AWS
       * Budgets directly and are **not** affected by the SNS confirmation state.
       */
      subscribers: alsoStop
        ? [{ subscriptionType: 'SNS', address: this.topic.topicArn }]
        : [{ subscriptionType: 'EMAIL', address: this.alertEmail }],
    };
  }
}
