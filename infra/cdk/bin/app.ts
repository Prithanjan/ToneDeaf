#!/usr/bin/env node
/**
 * SIH26104 Voice Integrity Control Plane — CDK app.
 *
 * **Six stack files: five in a strict dependency chain, plus one standalone.**
 *
 *   NetworkStack → DataStack → SecretsStack → ComputeStack → EdgeStack
 *   CostSafetyStack                                    [STANDALONE — no chain position]
 *
 * The order of the five is forced by cross-stack references (VPC id, Cloud Map namespace, secret
 * ARNs); it is not a style preference. `CostSafetyStack` reads nothing from the other five, so *when*
 * it is deployed is a policy decision rather than a technical one — and the policy is **immediately
 * after `DataStack`**, because the cost-safety plane exists to be armed before anyone can flip
 * `deployRuntime=true`. See architecture.md §4.1 and memory.md D-14.
 *
 * Deliberately NOT expressed as `costSafety.addDependency(dataStack)`. A hard dependency would mean a
 * `CostSafetyStack` failure blocks `SecretsStack` and everything after it — and a cost guardrail that
 * can block a deploy is a guardrail people delete. The ordering is a documented operational sequence
 * (aws-setup-instructions.md §7), enforced by the deploy runbook, not by the graph.
 *
 * ⚠️ The "immediately after DataStack" placement is a reconciliation of a contradiction *inside* the
 * source material, flagged as open decision H-5. A human confirms or overrides it before Phase 2.
 */
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { NetworkStack } from '../lib/network-stack';
import { DataStack } from '../lib/data-stack';
import { SecretsStack } from '../lib/secrets-stack';
import { ComputeStack } from '../lib/compute-stack';
import { EdgeStack } from '../lib/edge-stack';
import { CostSafetyStack } from '../lib/cost-safety-stack';

const app = new cdk.App();

/**
 * Region is read from context, not from the ambient environment.
 *
 * `CDK_DEFAULT_REGION` follows whatever the operator's shell happens to be configured for, and a
 * deploy that lands in the wrong region is not a visible failure — it is a second, parallel, working
 * stack nobody is watching and nobody tears down. Region discipline is a cost control as much as a
 * correctness one (aws-setup-instructions.md §0.2).
 */
const region = app.node.tryGetContext('region') ?? 'ap-south-1';
const account = process.env.CDK_DEFAULT_ACCOUNT;
const env: cdk.Environment = { account, region };

/**
 * THE cost control. Defaults to `false`, and the default is the point.
 *
 * When false: both ECS services synth with `desiredCount: 0` and the GPU ASG with min/max/desired all
 * 0. `cdk deploy` therefore cannot start GPU spend, which is what makes "no `git push` can start GPU
 * spend" (rules.md R-29) a property of the infrastructure rather than a rule people remember.
 *
 * Parsed strictly against the string `'true'`. `--context deployRuntime=1`, `=yes`, or `=TRUE` all
 * evaluate to false, which is the safe direction for a typo to fall in. Note that `tryGetContext`
 * returns the boolean `false` from cdk.json but a *string* from `--context`, so both forms are
 * handled here rather than in five stacks.
 */
const raw = app.node.tryGetContext('deployRuntime');
const deployRuntime = raw === true || raw === 'true';

if (deployRuntime) {
  // Printed on the one path that can cost money, so it appears in the CI log above the diff a human
  // is about to approve.
  cdk.Annotations.of(app).addWarning(
    'deployRuntime=true — this synth CAN start GPU spend. Confirm CostSafetyStack is deployed and ' +
      'armed, and that stop-runtime.yml is run at the end of the session (rules.md R-30).',
  );
}

const tags = {
  Project: 'sih26104',
  Component: 'voice-integrity-control-plane',
  // Tagged on every resource so the Budget can be filtered and, more importantly, so an orphan is
  // findable during teardown. An untagged resource that survives teardown bills until someone
  // notices.
  ManagedBy: 'cdk',
};

const network = new NetworkStack(app, 'NetworkStack', {
  env,
  tags,
  description: 'SIH26104 1/5 — VPC, subnets, one NAT gateway, deny-by-default security groups',
});

const data = new DataStack(app, 'DataStack', {
  env,
  tags,
  description: 'SIH26104 2/5 — RDS PostgreSQL 16 db.t4g.micro, private, encrypted, single-AZ',
  vpc: network.vpc,
  databaseSecurityGroup: network.databaseSecurityGroup,
});
data.addStackDependency(network);

/**
 * Standalone. Constructed here, in the position it is deployed in, so that reading this file gives
 * the operational sequence — but with **no** `addDependency` call, for the reason in the file header.
 *
 * **The one stack that is not in `region`.** AWS Budgets is a global service operating out of
 * us-east-1, so the budget and its SNS topic go there and the `RuntimeStopper` Lambda acts
 * cross-region on `ap-south-1`. This is the documented single-region exception (cdk.json `// region`),
 * and it is safe precisely *because* the stack is standalone: a cross-region reference between two
 * stacks needs a concrete account at synth time, and this stack has no cross-stack references at all.
 */
const costSafety = new CostSafetyStack(app, 'CostSafetyStack', {
  env: { account, region: 'us-east-1' },
  tags,
  description:
    'SIH26104 standalone — Budget → SNS → RuntimeStopper Lambda. Deploy immediately after DataStack (H-5)',
  monthlyBudgetUsd: Number(app.node.tryGetContext('monthlyBudgetUsd') ?? 100),
  alertEmail: app.node.tryGetContext('budgetAlertEmail') ?? 'CHANGE_ME@example.invalid',
  // Where the runtime it stops actually lives. Passed explicitly because a boto3 client with no
  // region defaults to the Lambda's own — us-east-1 — and would find nothing to stop.
  targetRegion: region,
});
void costSafety; // referenced only to make the deliberate absence of addDependency explicit

const secrets = new SecretsStack(app, 'SecretsStack', {
  env,
  tags,
  description: 'SIH26104 3/5 — references the Phase-0 Secrets Manager entries by ARN',
});
secrets.addStackDependency(data);

const compute = new ComputeStack(app, 'ComputeStack', {
  env,
  tags,
  description:
    'SIH26104 4/5 — ECS cluster, GPU capacity provider (desired 0), Gateway + Scorer task defs, Cloud Map',
  deployRuntime,
  vpc: network.vpc,
  gatewaySecurityGroup: network.gatewaySecurityGroup,
  scorerSecurityGroup: network.scorerSecurityGroup,
  albSecurityGroup: network.albSecurityGroup,
  database: data.database,
  databaseUrl: secrets.databaseUrl,
  ticketSigningKey: secrets.ticketSigningKey,
  hmacKey: secrets.hmacKey,
  auditChainKey: secrets.auditChainKey,
  gatewayImageDigest: app.node.tryGetContext('gatewayImageDigest') ?? '',
  scorerImageDigest: app.node.tryGetContext('scorerImageDigest') ?? '',
  /**
   * Empty by default, and empty is safe: the Gateway's origin validator rejects an empty allow-list
   * and the process refuses to boot. Filled in on the second pass, after EdgeStack has produced a
   * CloudFront domain — ComputeStack cannot read it directly, because EdgeStack depends on this
   * stack's Gateway service and the reference would be a cycle.
   */
  allowedOrigins: app.node.tryGetContext('allowedOrigins') ?? '',
  jwtIssuer: app.node.tryGetContext('jwtIssuer') ?? '',
  jwtJwksUrl: app.node.tryGetContext('jwtJwksUrl') ?? '',
  jwtAudience: app.node.tryGetContext('jwtAudience') ?? 'sih26104-gateway',
  /**
   * Stamped into the parity set and every audit row. `unknown` is honest rather than convenient — a
   * fabricated commit on an audit row is worse than an absent one, because it looks checkable.
   *
   * `||`, not `??`: cdk.json ships this key as an empty string, and `??` only falls through on
   * null/undefined, so `??` here would pin every audit row to `""`.
   */
  gitCommit: app.node.tryGetContext('gitCommit') || process.env.GITHUB_SHA || 'unknown',
});
compute.addStackDependency(secrets);

const edge = new EdgeStack(app, 'EdgeStack', {
  env,
  tags,
  description: 'SIH26104 5/5 — private S3 + OAC, internal ALB, CloudFront with VPC origin. DEPLOY LAST',
  vpc: network.vpc,
  albSecurityGroup: network.albSecurityGroup,
  gatewayAlb: compute.gatewayAlb,
});
edge.addStackDependency(compute);

app.synth();
