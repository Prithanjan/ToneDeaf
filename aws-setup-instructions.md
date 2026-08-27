# AWS Setup Instructions — Step by Step

**Status:** Operational runbook. Follow in order; the order is forced by cross-resource
dependencies.
**Region:** `ap-south-1` (Mumbai) for everything except the two global services noted in §0.2.
**Owner:** Pair A (Platform/Infra). Pairs B and C observe and approve the cost checkpoints.
**Companions:** [README.md](README.md) · [architecture.md](architecture.md) · [phases.md](phases.md) · [prd.md](prd.md) · [rules.md](rules.md)

> ### ⚠️ Read before your first command
> 1. **Runtime is zero by default** ([rules.md](rules.md) R-28). Every step below is designed so
>    that completing the entire setup costs *almost nothing per hour*. The GPU only runs when a
>    human explicitly turns it on in §11.
> 2. **§2 (GPU quota) is time-critical and is a Phase 0 Definition-of-Done blocker** — see
>    [phases.md](phases.md) §1.4, row `H-2`. Do it first, even before the IAM work. AWS quota
>    increases can take longer than three days, and nothing in Phase 3 works without it.
> 3. **Nothing here uses `AdministratorAccess`** or long-lived access keys. CI reaches AWS by
>    GitHub OIDC only (§3).
> 4. Charges you *will* incur immediately after §7: **1 NAT Gateway** (~₹3–4/hr) and **RDS
>    `db.t4g.micro`** (~₹1.5/hr). If you need to pause for a day, see §14.2.
> 5. Every `<placeholder>` below is deliberately fake. There is no real account ID, ARN, thumbprint
>    substitute, or secret value anywhere in this repository ([rules.md](rules.md) R-34).

---

## 0. Prerequisites & Tooling Setup

### 0.1 Local Tooling (Cross-Platform)

Ensure core CLI tools are installed on your development workstation:

#### Windows (PowerShell)
```powershell
# 1. AWS CLI v2 (installed at C:\Users\KIIT\AppData\Local\Programs\Amazon\AWSCLIV2\aws.exe)
irm 'https://awscli.amazonaws.com/v2/install.ps1' | iex
aws --version    # expect aws-cli/2.36.x or newer

# 2. GitHub CLI
winget install --id GitHub.cli
gh --version

# 3. Node.js 20+ & AWS CDK v2
npm install -g aws-cdk@2.266.0
cdk --version    # expect 2.266.0

# 4. Docker Engine / Docker Desktop
docker buildx version
```

#### Linux / macOS (Bash)
```bash
# 1. AWS CLI v2
curl -fsSL 'https://awscli.amazonaws.com/v2/install.sh' | bash
aws --version    # expect aws-cli/2.x

# 2. GitHub CLI
# Ubuntu/Debian: sudo apt install gh
# macOS: brew install gh
gh --version

# 3. Node.js 20+ & AWS CDK v2
npm install -g aws-cdk@2.266.0
cdk --version    # expect 2.266.0

# 4. Docker & Buildx
docker buildx version
```

---

### 0.2 Agent Toolkit for AWS Setup (AI Coding Agents & AWS MCP Server)

The **Agent Toolkit for AWS** provides AI coding assistants (Antigravity, Cursor, Claude Code, Codex, Kiro) with native AWS skills, MCP tool servers, and well-architected guardrails.

#### Step 1: Authenticate with AWS CLI
```powershell
# Windows (PowerShell)
aws login --profile tonedeaf-dev
# A browser window opens for authentication.
```
```bash
# Linux / macOS (Bash)
aws login --profile tonedeaf-dev
```

#### Step 2: Verify STS Caller Identity
```powershell
# Windows (PowerShell)
aws sts get-caller-identity --profile tonedeaf-dev
```
```bash
# Linux / macOS (Bash)
aws sts get-caller-identity --profile tonedeaf-dev
```

#### Step 3: Initialize Agent Toolkit & Install Skills
Run the configuration command to install agent skills and configure the AWS MCP Server connection.
> **Note:** The Agent Toolkit control-plane service is hosted in `us-east-1`. Always pass `--region us-east-1` for agent-toolkit commands regardless of your workload deployment region.

```powershell
# Windows (PowerShell)
aws configure agent-toolkit --yes --region us-east-1 --profile tonedeaf-dev
```
```bash
# Linux / macOS (Bash)
aws configure agent-toolkit --yes --region us-east-1 --profile tonedeaf-dev
```

#### Step 4: Verify Installed AWS Skills
```powershell
# List available skills in repository
aws agent-toolkit list-available-skills --region us-east-1 --profile tonedeaf-dev

# List installed skills on current workstation
aws agent-toolkit list-installed-skills --region us-east-1 --profile tonedeaf-dev
```

#### Step 5: AI Coding Rules & MCP Server Integration
The repository includes pre-configured rule files for AI coding agents:
- **Project Root**: [`AGENTS.md`](AGENTS.md) and [`CLAUDE.md`](CLAUDE.md)
- **AWS Rule Definitions**: [`.aws/rules/aws-agent-rules.md`](.aws/rules/aws-agent-rules.md) (advanced experience) and [`.aws/rules/aws-starter-rules.md`](.aws/rules/aws-starter-rules.md) (starter experience)

**Agent Toolkit Troubleshooting**:

| Symptom | Cause | Resolution |
|---|---|---|
| `Profile is already configured with Access Key` | Static credentials exist in `~/.aws/credentials` | Use named profile `--profile <name>` or remove static keys for browser sign-in |
| `aws: command not found` | AWS CLI path not refreshed in shell session | Windows: Restart PowerShell or check `$env:LOCALAPPDATA\Programs\Amazon\AWSCLIV2` |
| `Exit code 253 / interactive terminal required` | Non-interactive subshell invocation | Add `--yes` flag: `aws configure agent-toolkit --yes --region us-east-1 --profile <name>` |
| `Unable to locate credentials / ExpiredToken` | Temporary session token expired | Re-run `aws login --profile <name>` |

---

### 0.3 Region Discipline & Environment Configuration

| Resource Layer | Region | Architectural Rationale |
|---|---|---|
| **Workload Infrastructure** (VPC, ECS Fargate, ECS GPU ASG, RDS PostgreSQL, Valkey/Redis, Secrets Manager, Cognito) | `ap-south-1` (Mumbai) | Ultra-low latency voice packet processing, Indian regulatory compliance (DPDP 2023) |
| **Edge Distribution** | Global (CloudFront) | Edge caching of static PWA assets; WebSocket reverse-proxy origin routed to `ap-south-1` |
| **ACM TLS Certificate** | `us-east-1` (N. Virginia) | CloudFront distributions require ACM certificates provisioned in `us-east-1` |
| **AWS Budgets & Cost Anomaly** | `us-east-1` (Global Endpoint) | Account-level billing and budget notification alarms |
| **Agent Toolkit Control Plane** | `us-east-1` | AWS Agent Toolkit and MCP server registration endpoint |

#### Configure Environment Variables

```powershell
# Windows (PowerShell)
aws configure set region ap-south-1 --profile tonedeaf-dev
aws configure set output json --profile tonedeaf-dev
$env:AWS_PROFILE = "tonedeaf-dev"
$env:AWS_REGION = "ap-south-1"
$env:ACCOUNT_ID = (aws sts get-caller-identity --query Account --output text --profile tonedeaf-dev)
Write-Host "Configured Account: $env:ACCOUNT_ID in $env:AWS_REGION"
```

```bash
# Linux / macOS (Bash)
aws configure set region ap-south-1 --profile tonedeaf-dev
aws configure set output json --profile tonedeaf-dev
export AWS_PROFILE=tonedeaf-dev
export AWS_REGION=ap-south-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --profile tonedeaf-dev)
echo "Configured Account: ${ACCOUNT_ID} in ${AWS_REGION}"
```

---

## 1. Record the account cost baseline  ⟨H-3⟩

Confirm the **Paid Plan** and the remaining credit balance. This is a judge-facing artifact, not
bookkeeping.

```bash
# Credits are not fully exposed via CLI — capture the console view.
# Billing console → Credits → screenshot
aws ce get-cost-and-usage \
  --time-period Start=$(date -d '1 month ago' +%Y-%m-01),End=$(date +%Y-%m-%d) \
  --granularity MONTHLY --metrics UnblendedCost
```

Write the result into `docs/manifests/aws_account_baseline.md`: account ID (last 4 only), plan
type, credit balance + expiry, screenshot filename, date, who checked.

---

## 2. GPU quota — DO THIS FIRST  ⟨H-2, Phase 0 DoD blocker, time-critical⟩

`g4dn.xlarge` is On-Demand G-family capacity. A brand-new account frequently has a **0 vCPU** quota
for it, which means the ASG will silently fail to launch on Day 3.

> This is the **single most avoidable failure mode in the whole plan.** The blueprint files it as a
> Day-5 risk; that is wrong and is superseded — the increase request is filed in **Phase 0**. The DoD
> row is satisfied by a **request ID**, not by an approval, because filing is the only part of this
> the team controls. See [phases.md](phases.md) §1.4.

```bash
# Check the current limit. g4dn.xlarge = 4 vCPUs, so you need >= 4.
aws service-quotas get-service-quota \
  --service-code ec2 \
  --quota-code L-DB2E81BA \
  --region ap-south-1
# L-DB2E81BA = "Running On-Demand G and VT instances" (measured in vCPUs)
```

If `Value` is less than `4`, request an increase **now**:

```bash
aws service-quotas request-service-quota-increase \
  --service-code ec2 \
  --quota-code L-DB2E81BA \
  --desired-value 8 \
  --region ap-south-1
# Record the RequestId in docs/manifests/aws_account_baseline.md
```

Ask for `8` rather than `4` — it gives headroom for one replacement instance during a failed deploy
without a second quota round-trip. Do **not** ask for more; [rules.md](rules.md) R-32 caps the demo
at one GPU.

Then verify the instance type actually exists in your target AZs:

```bash
aws ec2 describe-instance-type-offerings \
  --location-type availability-zone \
  --filters Name=instance-type,Values=g4dn.xlarge \
  --region ap-south-1 --query 'InstanceTypeOfferings[].Location'
```

Use the returned AZs in `NetworkStack`. If `g4dn.xlarge` is unavailable in an AZ you selected, the
ASG cannot launch there regardless of quota.

---

## 3. GitHub OIDC provider and the deploy role

No long-lived access keys. GitHub Actions assumes a role via OIDC.

### 3.1 Create the OIDC provider

```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
```

> The thumbprint is no longer validated by AWS for this provider (IAM uses the library of trusted
> CAs), but the API still requires the field. If the call fails with
> `EntityAlreadyExists`, the provider already exists — continue.

### 3.2 Trust policy — render the committed one, do not hand-write it

The trust policy is committed at `infra/iam/gh-actions-trust-policy.json`. **Do not write your own**, and
in particular do not use a `sub` pattern ending in `:*` — that admits every branch and every pull request
from every fork, and it is the single most common OIDC misconfiguration. The committed file restricts
`sub` to main-branch pushes and the `aws-deploy` environment, which is what makes a `git push` unable to
touch AWS at all (`rules.md` R-29) rather than merely unable to start GPU spend.

**Both policy files carry `"//"` annotation keys and CANNOT be passed to the AWS CLI as-is.** IAM rejects
the whole document with `MalformedPolicyDocument: Syntax errors in policy`, naming no key and no line, so
it reads like a corrupt file. Render them first — the renderer strips the comments, substitutes the
placeholders, validates against IAM's grammar whitelist, and **refuses if any placeholder is left over**:

```bash
python scripts/render_iam_policies.py \
  --account-id "$ACCOUNT_ID" --github-owner "<ORG>" --github-repo "<REPO>"
```

That last guarantee matters most here. An unsubstituted `<AWS_ACCOUNT_ID>` in a *trust* policy is not a
loud failure: the ARN simply matches nothing, `create-role` succeeds, and every deploy fails later with an
OIDC error that points at GitHub rather than at this step.

Output lands in `infra/iam/rendered/`, which is git-ignored twice over (by `.gitignore` and by a
`.gitignore` the renderer writes into the directory itself) because it embeds your real account id.

```bash
aws iam create-role --role-name gh-actions-deploy-role \
  --assume-role-policy-document file://infra/iam/rendered/gh-actions-trust-policy.json \
  --description "SIH26104 CI deploy role - OIDC only, no keys"
```

**The role name is not a free choice.** `gh-actions-deploy-role` is referenced in 18 places — every
`Assume … via OIDC` step across five workflows, `architecture.md`'s trust diagram, and `secret-scan.yml`'s
detector fixtures. Renaming it means editing all of them.

### 3.3 Permissions — least privilege, not `AdministratorAccess`

```bash
aws iam put-role-policy --role-name gh-actions-deploy-role \
  --policy-name sih26104-deploy \
  --policy-document file://infra/iam/rendered/gh-actions-deploy-policy.json
```

Note `rendered/` — see the warning in §3.2. Attaching the annotated source fails.

**`infra/iam/gh-actions-deploy-policy.json` is authoritative; the summary below merely describes it.**
That distinction has already caused two bugs: `deploy-runtime.yml` and `stop-runtime.yml` both reasoned
about their own behaviour from an earlier, shorter version of this list and got it wrong — one of them told
operators that database migrations could not be run from CI for a permission reason that was never true.
**If you need to know whether this role can do something, read the JSON, not this section.**

Thirteen statements. What they grant, and the three that are worth understanding:

| Statement | Grants |
|---|---|
| `AssumeCdkBootstrapRoles` | `sts:AssumeRole` on the four `cdk-hnb659fds-*` roles, in `ap-south-1` **and** `us-east-1` |
| `ReadCdkBootstrapVersion` | `ssm:GetParameter` on the bootstrap version parameter |
| `EcrLoginRequiresWildcard` | `ecr:GetAuthorizationToken` (no resource types exist for it) |
| `PushProjectImages` | 8 ECR actions on the three project repos only |
| `StopRuntime` | `ecs:UpdateService` **and** `autoscaling:UpdateAutoScalingGroup` |
| `VerifyAndMigrateScoped` | `ecs:Describe*` + `ecs:RunTask`, scoped to the cluster and the `gateway-migrate` family |
| `ReadOnlyCallsWithoutResourceLevelSupport` | 4 Describe/List actions IAM cannot scope |
| `RegisterDigestPinnedTaskDefinitions` | `ecs:RegisterTaskDefinition` |
| `ReadScorerLogsForProviderCheck` | `logs:FilterLogEvents` on `/ecs/sih26104/*` |
| `PassEcsTaskRolesOnly` | `iam:PassRole` on `ComputeStack-*`, conditioned on `ecs-tasks.amazonaws.com` |
| `InvokeRuntimeStopper` | `lambda:InvokeFunction` on the one stopper function |
| `DenyOutsideApprovedRegions` | **Deny** outside `ap-south-1`/`us-east-1` |
| `DenySelfEscalationAndLongLivedCredentials` | **Deny** on 9 IAM actions |

**Statement 1 is the entire deploy grant, and it is four `sts:AssumeRole` calls.** CDK v2 does not create
resources with the caller's credentials — it assumes the bootstrap roles and CloudFormation acts through
its own execution role. So the privilege boundary is the CFN execution role, which *is* broadly
privileged. This policy does not make a malicious workflow harmless; it makes it auditable.

**`RegisterDigestPinnedTaskDefinitions` is a mutation with `Resource: "*"`, deliberately.**
`RegisterTaskDefinition` has no resource types. Its real control is `PassEcsTaskRolesOnly`: registering a
definition is inert unless a role can be passed to it, and that is bounded to `ComputeStack-*`.

**`ReadScorerLogsForProviderCheck` exists so R-45 is actually enforced.** Without it, §11.1's
GPU-provider check is a manual step CI reports as "not verified" on every run — and a silent CPU fallback
leaves the task *healthy* while invalidating every latency number measured that day.

Three things to verify with your own eyes before attaching:
- `iam:PassRole` has a `Resource` of named role ARN patterns, **not** `"*"`. `PassRole: *` is a
  privilege-escalation primitive: it would let this role hand *any* role to a service it controls the
  input of, then read that role's credentials out of the task it just launched.
- `secretsmanager:GetSecretValue` is scoped to the **five** `sih26104/*` secrets (§6) — not four.
- Exactly **three** `Allow` statements carry `Resource: "*"`, and one of them is the `RegisterTaskDefinition`
  mutation above. If a fourth appears, something was widened. Note that a `Resource` array mixing `"*"`
  with specific ARNs grants everything: IAM evaluates the list as OR.

Record the role ARN as the GitHub repo variable `AWS_DEPLOY_ROLE_ARN`:

```bash
gh variable set AWS_DEPLOY_ROLE_ARN --body "arn:aws:iam::$ACCOUNT_ID:role/gh-actions-deploy-role"
gh variable set AWS_REGION --body "ap-south-1"
gh variable set ECR_REGISTRY --body "$ACCOUNT_ID.dkr.ecr.ap-south-1.amazonaws.com"
```

### 3.4 The GitHub side of the same boundary

The IAM work above is only half of the control. Set these on the repository in Phase 0, before three
pairs have pushed to `main`:

```bash
# Branch protection on main: PR required, 1 review minimum, two required checks
gh api -X PUT repos/<ORG>/sih26104-voice-integrity/branches/main/protection \
  --input - <<'JSON'
{
  "required_status_checks": { "strict": true, "contexts": ["contract-test", "secret-scan"] },
  "required_pull_request_reviews": { "required_approving_review_count": 1 },
  "enforce_admins": true,
  "restrictions": null
}
JSON
```

| Setting | Value | Why |
|---|---|---|
| Repository | `sih26104-voice-integrity`, **private** | The OIDC trust policy in §3.2 matches `repo:<ORG>/sih26104-voice-integrity:*`. **Renaming the repo breaks CI's ability to assume the deploy role** |
| Required reviews on `main` | ≥ 1 | Plus the `contracts/` two-key rule (one Pair B + one Pair C) via `CODEOWNERS` ([rules.md](rules.md) R-22) |
| Required checks | `contract-test`, `secret-scan` | The PDF's minimum. `privacy-tests` is added on top by [phases.md](phases.md) §1.1 and stays |
| Workflows | **One per service** — `gateway-ci.yml`, `scorer-ci.yml`, `pwa-ci.yml`, path-filtered on `<service>/**` + `contracts/**` | A PWA change must not rebuild and re-push the scorer image |
| `permissions:` block | `id-token: write`, `contents: read` | `id-token: write` is what makes OIDC work at all; omitting it produces a confusing `Credentials could not be loaded` failure |
| Promotion between environments | **By image digest, never a rebuild** ([rules.md](rules.md) R-56) | A rebuild from the same tag produces different bytes, so the digest a manifest recorded no longer describes what is running |
| Auto-deploy | **None in Phase 1–2.** CI stops at `docker push` | §11 is the only path that starts GPU spend ([rules.md](rules.md) R-29) |

---

## 4. CDK bootstrap

**Two regions, not one.** This is the step most likely to be done half-way, because everything you can
name in this project lives in `ap-south-1`.

```bash
cdk bootstrap aws://$ACCOUNT_ID/ap-south-1
cdk bootstrap aws://$ACCOUNT_ID/us-east-1
```

This creates the `CDKToolkit` stack in each: an S3 asset bucket, an ECR repo for CDK assets, and the
`cdk-*` execution roles. Do this once per account+region.

`us-east-1` is required because **`CostSafetyStack` deploys there**, not to `ap-south-1`. AWS Budgets is a
global service operating out of `us-east-1`, and `infra/cdk/bin/app.ts:106` pins that stack's environment
accordingly (`env: { account, region: 'us-east-1' }`) while every other stack takes the `region` context
value. A stack cannot be deployed into a region that has never been bootstrapped.

> **⚠️ Do NOT add `--region us-east-1` to the `CostSafetyStack` deploy command in §7.** The region is a
> property of the stack, fixed at synth time by that `env`, and `cdk deploy` has no `--region` flag to
> override it. Adding one does not help and reads as though the region were still undecided. If you want
> to confirm where it will land before deploying, `npx cdk synth CostSafetyStack` and check the
> `aws:cdk:region` in the resulting template metadata, or just look at `bin/app.ts:106`.

The failure if you skip the second bootstrap is not obvious from its message: the `CostSafetyStack` deploy
fails on a missing SSM bootstrap-version parameter in `us-east-1`, which reads like a permissions problem
in an account that has manifestly been bootstrapped. Note that
`infra/iam/gh-actions-deploy-policy.json` already grants `sts:AssumeRole` on the `cdk-hnb659fds-*` roles
in **both** regions and fences the role to both (§3.3, statements 1 and 12) — the permission model
anticipated the two-region bootstrap from the start, so if a deploy is failing here it is the bootstrap
that is missing, not the policy.

---

## 5. ECR repositories

**Three repositories, not two.**

```bash
for repo in sih26104/gateway sih26104/scorer-gpu sih26104/scorer-cpu; do
  aws ecr create-repository --repository-name "$repo" \
    --image-scanning-configuration scanOnPush=true \
    --image-tag-mutability IMMUTABLE \
    --encryption-configuration encryptionType=AES256
done
```

| Repository | Consumed by | Why it is separate |
|---|---|---|
| `sih26104/gateway` | ECS Gateway service + local Compose | One image serves both tiers; the tier is config, not a branch ([rules.md](rules.md) R-04) |
| `sih26104/scorer-gpu` | ECS Scorer service on `g4dn.xlarge` | Installs `onnxruntime-gpu`, CUDA/cuDNN pinned |
| `sih26104/scorer-cpu` | Local CPU fallback tier | Installs plain `onnxruntime`. **The two scorer images cannot be byte-identical**, so they do not share a repository — the documented parity exception ([architecture.md](architecture.md) §5.1, [rules.md](rules.md) R-06) is visible in the registry instead of hidden behind a tag convention |

The parity set that *must* match across the two scorer images — model ONNX SHA-256, calibration
SHA-256, application source, contract hashes — is enumerated in [architecture.md](architecture.md)
§5.1. Nobody claims the images are identical; the claim is that the parity set is.

Two deliberate choices:
- **`IMMUTABLE` tags** — an image digest is the deployment unit ([rules.md](rules.md) R-51, R-56). A
  mutable tag lets a rebuild silently change what a manifest claims was deployed.
- **`scanOnPush`** — free basic scanning; feeds the security matrix on Day 4.

Add a lifecycle policy so a week of CI does not accumulate cost:

```bash
for repo in sih26104/gateway sih26104/scorer-gpu sih26104/scorer-cpu; do
  aws ecr put-lifecycle-policy --repository-name "$repo" --lifecycle-policy-text '{
    "rules":[{"rulePriority":1,"description":"keep last 15",
      "selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":15},
      "action":{"type":"expire"}}]}'
done
```

---

## 6. Secrets Manager placeholders  ⟨must precede any task definition⟩

Create these **now**, before any ECS task definition references them. Otherwise you hit an ordering
deadlock: the stack needs the ARN, the ARN needs the secret.

**There are FIVE, not four.** The canonical list is `infra/cdk/lib/secrets-stack.ts:26-42` — if this
section and that file ever disagree, the file wins. Four are random values; `sih26104/database-url` is
not, and needs its own step below.

```bash
for name in sih26104/db-password sih26104/ticket-signing-key \
            sih26104/hmac-key sih26104/audit-chain-key; do
  aws secretsmanager create-secret --name "$name" \
    --description "SIH26104 - PLACEHOLDER, rotate before any real session" \
    --secret-string "CHANGE_ME_$(openssl rand -hex 16)"
done
```

### The fifth secret: `sih26104/database-url`

It holds a full connection URL, not a random value, so it cannot be generated like the others.

Why it exists at all, given `sih26104/db-password` already holds the password: ECS `secrets:` injects
exactly one Secrets Manager value per environment variable and **cannot interpolate** a password into a
URL. `Settings.database_url` (`gateway/app/config.py:66`) wants the assembled URL. The alternatives were
an entrypoint shim that builds the URL — a new moving part, and the password lands in a process argument
list — or changing the app's config contract. A fifth secret is the smallest honest option. The reasoning
is recorded at `infra/cdk/lib/compute-stack.ts:367-377`.

> **⚠️ The scheme is `postgresql://` — NOT `postgresql+asyncpg://`.** This is the one detail in this
> section that fails closed and loudly, so get it right the first time. The `+driver` suffix is
> *SQLAlchemy dialect* syntax, and **this project does not use SQLAlchemy anywhere in the serving path.**
> The Gateway calls `asyncpg.create_pool()` directly (`gateway/app/main.py:153`), and asyncpg 0.30.0
> validates the scheme itself, rejecting anything that is not exactly `postgresql` or `postgres`:
>
> ```
> ValueError: invalid DSN: scheme is expected to be either "postgresql" or "postgres",
>             got 'postgresql+asyncpg'
> ```
>
> That is a crash in the Gateway's startup path, before it serves anything — so the symptom is an ECS
> task that pulls, starts, and dies in a loop, with a `ValueError` in CloudWatch that says nothing about
> Secrets Manager. Meanwhile every other secret is fine and `psql` with `sih26104/db-password` works
> perfectly, which is what makes this expensive to diagnose from the outside.
>
> The plain scheme is not a compromise — it is the only value **all three** consumers accept.
> Alembic normalizes any sync scheme *upward* to `postgresql+asyncpg://` and logs that it did
> (`audit/migrations/env.py:57`, regex at `:42`), and the retention worker strips the suffix *downward*
> before its own `asyncpg.connect()` (`audit/retention_worker.py:559`). Two independent authors already
> defended against the wrong form in opposite directions; `postgresql://` is the fixed point between
> them. Local Compose uses exactly this scheme for the same reason
> (`infra/compose/docker-compose.yml:95`).

```bash
aws secretsmanager create-secret --name sih26104/database-url \
  --description "SIH26104 - PLACEHOLDER, rotate with db-password before any real session" \
  --secret-string "postgresql://sih:CHANGE_ME@localhost:5432/sih26104"
```

> **⚠️ `sih26104/db-password` and `sih26104/database-url` contain the same password in two shapes, so
> rotate them TOGETHER.** Rotating one alone leaves the application unable to connect while `psql` from a
> one-shot task still works — or the reverse — and the symptom is an authentication error that looks like
> a networking problem. The password secret is the credential of record for a human; the URL is what the
> application reads.

**Rotate to real generated values before any real demo session.** Placeholders exist only to
unblock IaC wiring.

> ### ⚠️ Read this before running the block below — `sih26104/audit-chain-key` is rotate-once
>
> **Rotate `sih26104/audit-chain-key` exactly once, before the first real session, then never
> again** ([rules.md](rules.md) **R-58**). The chain is a keyed HMAC: rotating the key makes every
> prior `event_hash` unverifiable in one action, with no error at the time and no recovery
> afterwards. There is no re-anchoring procedure, because re-anchoring means recomputing hashes
> under the new key, which is indistinguishable from forging them.
>
> **This warning used to sit *below* the code block, and the chain key used to be inside the bulk
> loop.** That is the ordering that produces the accident — someone copies a three-secret loop,
> runs it a second time on Day 4 to "make sure the secrets are fresh," and destroys the evidence
> for every session recorded so far. The loop below therefore rotates **two** secrets, and the
> chain key is handled separately with a guard that refuses if any audit event already exists.
> Do not merge them back together.

```bash
# Rotation, when you are ready (before Day 3). TWO secrets — the chain key is NOT here.
for name in sih26104/ticket-signing-key sih26104/hmac-key; do
  aws secretsmanager put-secret-value --secret-id "$name" \
    --secret-string "$(openssl rand -base64 48)"
done
```

```bash
# The audit chain key, ONCE, before the first real session (rules.md R-58).
# Check first. If this returns anything other than 0, STOP — rotating now destroys that evidence.
psql "$(aws secretsmanager get-secret-value --secret-id sih26104/database-url \
  --query SecretString --output text)" -tAc 'SELECT count(*) FROM audit_event;'

# Only if the count above is 0:
aws secretsmanager put-secret-value --secret-id sih26104/audit-chain-key \
  --secret-string "$(openssl rand -base64 48)"
```

If the table does not exist yet, the `psql` call errors — that is also a safe state to rotate in,
because a table that does not exist holds no events. Distinguish "relation does not exist" from a
connection failure before concluding it is safe; a networking error is not evidence of an empty
table.

```bash
# The database pair, together — note this is NOT in the loop above, because both values must
# derive from the same generated password:
DB_PW="$(openssl rand -base64 32 | tr -d '/+=')"
aws secretsmanager put-secret-value --secret-id sih26104/db-password --secret-string "$DB_PW"
aws secretsmanager put-secret-value --secret-id sih26104/database-url \
  --secret-string "postgresql://sih:${DB_PW}@<RDS_ENDPOINT>:5432/sih26104"
unset DB_PW
```

`tr -d '/+='` is not cosmetic: base64 can emit `/`, `+` and `=`, all of which must be percent-encoded
inside a URL's userinfo field. Stripping them avoids a class of connection failure that presents as bad
credentials. Substitute the real `<RDS_ENDPOINT>` from `DataStack`'s outputs.

---

## 7. Deploy the stacks, in dependency order

**Six stack files: five in a strict dependency chain, plus one standalone.**
`NetworkStack → DataStack → SecretsStack → ComputeStack → EdgeStack` is forced by cross-stack
references (VPC ID, Cloud Map namespace, secret ARNs). `CostSafetyStack` reads nothing from the other
five, so its position is a **policy** decision — and the policy is *deploy it immediately after
`DataStack`* ([rules.md](rules.md) R-33).

```bash
cd infra/cdk
npm ci
npm run build
npx cdk synth --context deployRuntime=false      # verify runtime-zero BEFORE deploying
```

**Check the synth output before deploying.** Grep the template to confirm runtime-zero:

```bash
npx cdk synth ComputeStack --context deployRuntime=false \
  | grep -E 'DesiredCount|DesiredCapacity|MinSize|MaxSize'
# Every value must be 0. If any is not, stop and fix the stack.
```

Then deploy in order:

```bash
npx cdk deploy NetworkStack     --context deployRuntime=false
npx cdk deploy DataStack        --exclusively --context deployRuntime=false
npx cdk deploy CostSafetyStack  --exclusively    # ← standalone, but deployed HERE on purpose (R-33)
npx cdk deploy SecretsStack     --exclusively
npx cdk deploy ComputeStack     --exclusively --context deployRuntime=false
npx cdk deploy EdgeStack        --exclusively    # ← LAST: CloudFront takes ~15 min to propagate
```

> **⚠️ Reconciled source conflict — `H-5`, confirm with the team lead before running this.** The
> 2026-08-26 source PDF is internally inconsistent about when `CostSafetyStack` is deployed: its file
> listing says "standalone, deploy anytime after data-stack", its prose says "immediately after
> DataStack", and its own command listing places it after `ComputeStack`. **The sequence above takes
> the prose reading**, because deploying it after `ComputeStack` leaves a window in which the GPU ASG
> and both ECS services are deployable with no budget backstop armed — which inverts the control the
> stack exists to provide. Full rationale in [architecture.md](architecture.md) §4.1; the open
> decision is `H-5` in [prd.md](prd.md) §9.1.

### 7.1 What each stack creates

| Stack | Chain position | Resources | Cost while idle |
|---|---|---|---|
| `NetworkStack` | 1 of 5 | VPC, 2 public + 2 app-private + 2 data-private subnets, **1** NAT Gateway, deny-by-default SGs | NAT Gateway hourly + data processing |
| `DataStack` | 2 of 5 | RDS PostgreSQL 16 `db.t4g.micro`, private, encrypted, single-AZ, 1-day backup, `RemovalPolicy.DESTROY` | RDS hourly + storage |
| `CostSafetyStack` | **standalone** — deployed here by policy, depends on nothing | Budget → SNS topic → `RuntimeStopper` Lambda | ~zero |
| `SecretsStack` | 3 of 5 | References §6 secrets by ARN; grants read to task roles | secret-months |
| `ComputeStack` | 4 of 5 | ECS cluster, GPU capacity provider + ASG (**desired 0**), Gateway + Scorer task defs, Cloud Map namespace `sih26104.local`, **the internal ALB** + listener + target group | ~zero at desired 0 |
| `EdgeStack` | 5 of 5 | Private S3 + OAC, CloudFront distribution with VPC origin **pointing at ComputeStack's ALB**, and the CloudFront→ALB SG ingress rule (§9.3) | ~zero idle |

> **The internal ALB is created in `ComputeStack`, not `EdgeStack`** (`infra/cdk/lib/compute-stack.ts:552`;
> `EdgeStack` receives it as a prop at `edge-stack.ts:38`). This looks misplaced — an ALB is edge-shaped —
> and it is worth knowing why it is not: the ALB needs a target group holding the Gateway ECS service, so
> putting it in `EdgeStack` would make `EdgeStack` depend on `ComputeStack` for the service *and*
> `ComputeStack` depend on `EdgeStack` for the target group. That is a cycle. Ownership follows the target,
> and the reasoning is recorded at `ComputeStack.gatewayAlb`.
>
> The practical consequence: **the ALB's DNS name is a `ComputeStack` output, not an `EdgeStack` one.**
> Looking for it under the wrong stack is a two-minute detour at exactly the wrong moment.

Only one NAT Gateway is provisioned on purpose — it is a single point of failure and the plan
accepts that for a five-day demo. Note it in the retrospective rather than pretending otherwise.

### 7.2 Verify runtime-zero after deploy  ⟨R-28⟩

```bash
aws ecs describe-services --cluster sih26104 --services gateway scorer \
  --query 'services[].{name:serviceName,desired:desiredCount,running:runningCount}'
# expect desired: 0, running: 0 for both

aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names scorer-gpu-asg \
  --query 'AutoScalingGroups[].{min:MinSize,max:MaxSize,desired:DesiredCapacity}'
# expect 0 / 0 / 0
```

---

## 8. Database migration

The RDS instance is in a private subnet with no public IP, reachable only from the Gateway SG.
There is **no bastion and no SSH** ([rules.md](rules.md) R-36). Run migrations from a one-shot ECS
task on the same network, not from your laptop.

```bash
# One-shot Alembic task (task definition 'gateway-migrate' from ComputeStack)
aws ecs run-task --cluster sih26104 \
  --task-definition gateway-migrate \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$APP_SUBNET_A],securityGroups=[$GATEWAY_SG],assignPublicIp=DISABLED}" \
  --overrides '{"containerOverrides":[{"name":"migrate","command":["alembic","upgrade","head"]}]}'

# Follow it
aws logs tail /ecs/sih26104/gateway-migrate --follow
```

> **`--launch-type FARGATE`, not `EC2`.** `gateway-migrate` is a `FargateTaskDefinition`
> (`infra/cdk/lib/compute-stack.ts:654`) — it is the Gateway image with a different command, and the
> Gateway tier is Fargate. Only the *Scorer* is EC2, and only because Fargate has no GPU
> (`compute-stack.ts:437`). Passing `EC2` here fails with an incompatible-launch-type error, and the
> tempting next move — starting the GPU ASG so there is an EC2 instance for it to land on — spends the
> one `g4dn.xlarge` the budget allows ([rules.md](rules.md) R-32) on a database migration. Migrations
> need no GPU and must run with the runtime stopped.

Verify the deny-list against the *real* RDS schema, not just local Postgres:

```bash
aws ecs run-task --cluster sih26104 --task-definition gateway-migrate \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$APP_SUBNET_A],securityGroups=[$GATEWAY_SG],assignPublicIp=DISABLED}" \
  --overrides '{"containerOverrides":[{"name":"migrate","command":["pytest","-q","audit/tests/test_schema_allow_list.py"]}]}'
```

> **⚠️ This command will not work as written, and the reason is a deliberate property of the image, not a
> bug.** `pytest` is declared in `gateway/requirements-dev.txt`, **not** `gateway/requirements.txt`, and
> the runtime image is built from the latter — precisely so that a test runner is absent from anything that
> serves traffic. So `pytest` is not on the container's `PATH` and the task exits with a command-not-found.
>
> Two honest ways to get the assurance this step is reaching for:
>
> 1. **Run it against local Postgres instead**, where the dev dependencies exist:
>    `../.venv-ws/Scripts/python.exe -m pytest -q audit/tests/test_schema_allow_list.py`. This is where the
>    check normally belongs — the suite parses migration *text* rather than reflecting a live database
>    (`audit/tests/test_schema_allow_list.py:74-75` says so explicitly, and notes it must run where
>    SQLAlchemy is absent), so it does not actually need RDS to be meaningful.
> 2. **If you specifically want to assert against the deployed schema**, query it directly rather than
>    shipping a test runner into production. `psql` is reachable from the same one-shot task, and the
>    columns the deny-list forbids can be checked with a single `information_schema` query.
>
> Do **not** "fix" this by adding `pytest` to `requirements.txt`. That puts a test framework and its
> transitive dependencies into the serving image to make one documentation step convenient, and it is the
> kind of change that is never reverted.
>
> Note also the filename: the suite is `audit/tests/test_schema_allow_list.py`. Earlier drafts of this
> section called it `test_schema_denylist.py`, which does not exist — the schema control is expressed as an
> **allow-list**, and that is the stronger form (a column nobody anticipated is rejected by default rather
> than permitted). Do not confuse it with `audit/tests/test_deny_list.py`, which is a real but different
> suite: that one covers the *field* deny-list in the audit writer — the control that was found to be
> unreachable dead code (`memory.md` §4 BUG-1) — and it needs no database at all.

---

## 9. CloudFront + ALB VPC origin — the fiddly part

### 9.1 Why this shape

CloudFront **VPC origins** let a *private* ALB stay the origin while CloudFront is the only public
entry point. That gives one domain, browser-compatible TLS, PWA asset delivery, and WSS entry —
with no public ALB and no direct task ingress.

### 9.2 Behaviors — cache configuration is a correctness issue

| Path pattern | Origin | Cache policy | Origin request policy |
|---|---|---|---|
| `/api/*` | ALB VPC origin | **`CachingDisabled`** | `AllViewerExceptHostHeader` |
| `/ws/*` | ALB VPC origin | **`CachingDisabled`** | **must forward** `Sec-WebSocket-Key`, `Sec-WebSocket-Version`, `Sec-WebSocket-Protocol`, `Sec-WebSocket-Accept`, `Upgrade`, `Connection` |
| `/*` (default) | S3 + OAC | `CachingOptimized` | — |

Caching a `/api/*` response would serve one analyst's session to another. Failing to forward the
WebSocket headers makes the upgrade fail with an opaque 400 — the single most common
CloudFront-WSS mistake.

Viewer protocol policy: **`redirect-to-https`** on all behaviors. Allowed methods on `/api/*` and
`/ws/*`: `GET, HEAD, OPTIONS, PUT, POST, PATCH, DELETE`.

### 9.3 The CloudFront → ALB security-group binding (automated; manual fallback below)

After `EdgeStack` creates the VPC origin, CloudFront provisions a **service-managed security group** in
your VPC, named `CloudFront-VPCOrigins-Service-SG`. Traffic does not flow until that SG is allowed into
the ALB's inbound rules.

**`EdgeStack` now does this for you.** It is no longer a required manual step, and the section below is
kept only as a fallback. Two constructs make it work:

1. A lookup custom resource (`infra/cdk/lib/edge-stack.ts:228`) that calls
   `ec2:DescribeSecurityGroups` filtered on that group name — because the SG is created by CloudFront
   during the same deployment, so its id cannot be known at synth time.
2. A `CfnSecurityGroupIngress` in **`EdgeStack`** (`:264`), deliberately *not*
   `albSecurityGroup.addIngressRule(...)`. That distinction is load-bearing: `addIngressRule` attaches the
   rule to the security group's **own** stack, which is `NetworkStack`, and `NetworkStack` is deployed
   before `EdgeStack` — so it would need a value from a stack that does not exist yet. The result is a
   cross-stack cycle CDK refuses to synthesize.

Verify it landed, rather than assuming:

```bash
aws ec2 describe-security-groups --group-ids $ALB_SG_ID \
  --query 'SecurityGroups[0].IpPermissions[?FromPort==`8080`].UserIdGroupPairs[].GroupId'
```

That should return the CloudFront-managed SG id. If it returns empty, the lookup failed — fall back:

```bash
# 1. Find the CloudFront-managed SG (created in your VPC)
aws ec2 describe-security-groups \
  --filters Name=vpc-id,Values=$VPC_ID \
  --query 'SecurityGroups[?contains(GroupName,`CloudFront`)].{id:GroupId,name:GroupName}'

# 2. Allow it into the ALB SG on the origin port
aws ec2 authorize-security-group-ingress \
  --group-id $ALB_SG_ID \
  --protocol tcp --port 8080 \
  --source-group $CLOUDFRONT_MANAGED_SG_ID
```

`EdgeStack` also emits this exact command as a stack output (`edge-stack.ts:275`), pre-filled with the
real ALB SG id, so you do not have to reconstruct it under pressure.

**If you used the fallback, write the exact IDs and the date into
`docs/manifests/cloudfront_sg_bind.md`.** On Day 5 someone will ask why the ALB has a hand-edited rule,
and "documented fallback, §9.3, lookup custom resource failed on <date>" is the answer. It lives under
`docs/manifests/` alongside `aws_account_baseline.md` and `release_manifest.json` because it is
judge-facing evidence of a manual intervention. If the automation worked, there is nothing to record — the
rule is in the CloudFormation template and that *is* the record.

### 9.4 Verify the edge

```bash
export CF_DOMAIN=$(aws cloudformation describe-stacks --stack-name EdgeStack \
  --query 'Stacks[0].Outputs[?OutputKey==`DistributionDomainName`].OutputValue' --output text)

curl -sSI "https://$CF_DOMAIN/"                  # PWA index → 200 from S3
curl -sS  "https://$CF_DOMAIN/api/v1/healthz"    # → 503 until runtime is up. That is correct.
```

A 503 on `/api/v1/healthz` with runtime at desired=0 is the **expected** state. If you get a
CloudFront 403 instead, the S3 OAC or the behavior ordering is wrong.

WebSocket check (after §11 brings runtime up):

```bash
npx wscat -c "wss://$CF_DOMAIN/ws/v1/stream" -s sih-v1 -s "sih-ticket.$TICKET"
# Expect: rejection with AUTH_TICKET_INVALID for a bogus ticket — that proves the
# upgrade path works AND that ticket validation is enforced. Both facts in one test.
```

### 9.5 Custom domain (optional)

Only if you want a friendly name for the QR code. Request the certificate in **`us-east-1`**:

```bash
aws acm request-certificate --domain-name demo.<yourdomain> \
  --validation-method DNS --region us-east-1
```

Add the DNS validation record, then set the alternate domain name + certificate on the
distribution. Skip this if the default `*.cloudfront.net` domain is acceptable — one less
propagation delay on Day 5.

---

## 10. Cognito

`EdgeStack`/`SecretsStack` provisions the User Pool. Verify the demo posture:

```bash
aws cognito-idp describe-user-pool --user-pool-id $POOL_ID \
  --query 'UserPool.{mfa:MfaConfiguration,sms:SmsConfiguration,policies:Policies}'
```

Required demo configuration:
- **SMS disabled.** No phone numbers in the system ([rules.md](rules.md) R-15 forbids storing them
  anyway).
- **Software TOTP** supported for configured users.
- App client: **no client secret** (public browser client — a secret in a PWA bundle is not a
  secret).
- Auth flow: `ALLOW_USER_SRP_AUTH` for the MVP.

```bash
# Create the demo analyst users (do not use real personal emails)
aws cognito-idp admin-create-user --user-pool-id $POOL_ID \
  --username analyst-demo-1 --message-action SUPPRESS \
  --user-attributes Name=email,Value=analyst1@example.invalid Name=email_verified,Value=true
aws cognito-idp admin-set-user-password --user-pool-id $POOL_ID \
  --username analyst-demo-1 --password "$(openssl rand -base64 18)Aa1!" --permanent
```

> **Honesty requirement ([rules.md](rules.md) R-01):** the PWA uses **direct SRP**, not
> Authorization Code + PKCE. Present SRP truthfully as the controlled demo path. Do not describe
> the auth flow as PKCE on a slide.

Record the group/tenant claim contract now, even though the demo is single-tenant — the local JWKS
test issuer must mint the *same* claim shape so one JWT validation code path serves both tiers.

---

## 11. Turning the runtime ON  ⟨costs money — R-29⟩

**Never from a `git push`.** Only via the manual workflow:

```bash
gh workflow run deploy-runtime.yml \
  -f gateway_image_digest=sha256:... \
  -f scorer_image_digest=sha256:... \
  -f confirm_cost_aware=true
```

Preconditions, all of which must be true:
1. Pair B has confirmed the **ONNX parity gate in writing** ([phases.md](phases.md) §4.1).
2. `CostSafetyStack` is deployed and armed.
3. GPU quota is confirmed (§2).
4. Someone is watching, and knows the stop command.

What it does:

```bash
aws autoscaling update-auto-scaling-group --auto-scaling-group-name scorer-gpu-asg \
  --min-size 1 --max-size 1 --desired-capacity 1
aws ecs update-service --cluster sih26104 --service gateway --desired-count 1 --force-new-deployment
aws ecs update-service --cluster sih26104 --service scorer  --desired-count 1 --force-new-deployment
./scripts/wait_for_scorer_healthy.sh
```

### 11.1 Confirm the GPU is actually being used  ⟨R-45⟩

A silent CPU fallback invalidates every latency number you record. Check the banner:

```bash
aws logs tail /ecs/sih26104/scorer --since 5m | grep -E 'provider|model_sha|calibration_sha|detector_mode'
# MUST show CUDAExecutionProvider. If it shows CPUExecutionProvider, the deploy has FAILED
# even though the task is "healthy". Stop and fix the image.
```

Also confirm the ECS GPU prerequisites are met on the instance: GPU-optimized AMI,
`ECS_ENABLE_GPU_SUPPORT=true` in the instance user data, and `gpuCount=1` in the task definition's
resource requirements. `g4dn.xlarge` = 1 GPU, 16 GiB GPU memory, 4 vCPU, 16 GiB RAM.

---

## 12. Turning the runtime OFF  ⟨every session, no exceptions — R-30⟩

```bash
gh workflow run stop-runtime.yml -f confirm=true
```

Or manually — and verify, do not assume:

```bash
aws ecs update-service --cluster sih26104 --service gateway --desired-count 0
aws ecs update-service --cluster sih26104 --service scorer  --desired-count 0
aws autoscaling update-auto-scaling-group --auto-scaling-group-name scorer-gpu-asg \
  --min-size 0 --max-size 0 --desired-capacity 0

# VERIFY — this is the step people skip
aws ecs describe-services --cluster sih26104 --services gateway scorer \
  --query 'services[].{s:serviceName,d:desiredCount,r:runningCount}'
aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names scorer-gpu-asg \
  --query 'AutoScalingGroups[].{min:MinSize,max:MaxSize,desired:DesiredCapacity,instances:length(Instances)}'
aws ec2 describe-instances --filters Name=instance-type,Values=g4dn.xlarge \
  Name=instance-state-name,Values=running,pending \
  --query 'Reservations[].Instances[].InstanceId'
# The last command must return an empty list.
```

> **Zeroing `max-size` matters.** Setting only `desired-capacity=0` while `min-size=1` makes the
> ASG relaunch the instance immediately. This is exactly why a direct `ec2 stop-instances` is
> insufficient ([rules.md](rules.md) R-31).

---

## 13. Cost safety plane — verify it actually works

Four layers ([architecture.md](architecture.md) §7.1). Layer 1 (runtime-zero) is verified in **§7.2 of
this document**; layer 2 (manual `stop-runtime` after every session) is **§12 of this document**. Layers
3 and 4:

### 13.1 Budget → SNS → RuntimeStopper  ⟨layer 3⟩

```bash
aws budgets describe-budgets --account-id $ACCOUNT_ID \
  --query 'Budgets[].{name:BudgetName,limit:BudgetLimit,notifications:TimeUnit}'
```

Configure **both** an ACTUAL threshold (e.g. 60 % of the budget) and a FORECASTED threshold (e.g.
100 %). Forecast fires earlier and is the more useful of the two.

**Test the Lambda directly — do not wait for a real budget breach:**

```bash
aws lambda invoke --function-name RuntimeStopper \
  --payload '{"Records":[{"Sns":{"Message":"{\"budgetName\":\"manual-test\"}"}}]}' \
  --cli-binary-format raw-in-base64-out /tmp/out.json && cat /tmp/out.json

# Then confirm it actually zeroed things (run the §12 verification block)
```

Run this test on Day 3 with the runtime **up**, so you prove it can stop a *running* GPU, not just
an already-stopped one. That is the only version of the test that means anything.

### 13.2 The limits you must state out loud

**AWS Budgets are a delayed cost control, not an instantaneous circuit breaker.** State the actual
latency characteristic rather than the metaphor: budgets evaluate against Cost Explorer data, which
refreshes at most a few times a day, so an alert fires **hours** after the spend that triggered it. A
GPU left running overnight has already billed for the night by the time `RuntimeStopper` zeroes it.

The Budget path is a backstop for the case where a human forgot §12 — it is not the primary mechanism,
and saying otherwise on a slide is an overclaim ([rules.md](rules.md) R-30). The number to quote to a
judge is "manual stop after every session, verified; budget alarm as a bounded-loss backstop."

### 13.3 EventBridge Scheduler nightly stop — layer 4, Phase 4 target

Deferred, per [phases.md](phases.md) §8. When you add it:

```bash
aws scheduler create-schedule --name sih26104-nightly-stop \
  --schedule-expression "cron(30 23 * * ? *)" \
  --schedule-expression-timezone "Asia/Kolkata" \
  --flexible-time-window '{"Mode":"OFF"}' \
  --state DISABLED \
  --target '{"Arn":"<RuntimeStopper-ARN>","RoleArn":"<scheduler-exec-role-ARN>",
             "RetryPolicy":{"MaximumRetryAttempts":3},
             "DeadLetterConfig":{"Arn":"<dlq-arn>"}}'
```

Note `--state DISABLED`: enable it only during the demo window. Scheduler is timezone-aware with
60-second precision — fine for a cost stop, **not** a real-time safety control. It needs a
dedicated least-privilege execution role with target-invocation permission, a retry policy, and a
DLQ. Until all four exist, [rules.md](rules.md) R-01 says label it "not implemented."

---

## 14. Pausing and tearing down

### 14.1 Full teardown (after the demo)

```bash
# 1. Runtime to zero first (§12) — cdk destroy on a running ASG is slower and can strand ENIs
# 2. Reverse deploy order
npx cdk destroy EdgeStack --force
npx cdk destroy ComputeStack --force
npx cdk destroy SecretsStack --force
npx cdk destroy CostSafetyStack --force
npx cdk destroy DataStack --force        # RemovalPolicy.DESTROY — the DB is deleted
npx cdk destroy NetworkStack --force

# 3. Confirm
aws cloudformation list-stacks --stack-status-filter DELETE_COMPLETE \
  --query 'StackSummaries[?contains(StackName,`Stack`)].StackName'
```

Then check **Cost Explorer a day later** for residual charges — deleted resources can still bill
for the hours they existed, and an orphaned EIP or snapshot bills indefinitely.

Not removed by `cdk destroy`, delete by hand if you are done: ECR repositories (and their images),
Secrets Manager entries (7-day recovery window by default — use
`--force-delete-without-recovery` only if you are certain), CloudWatch log groups, the OIDC
provider, `gh-actions-deploy-role`, and the `CDKToolkit` stack.

### 14.2 Pausing overnight without tearing down

The two idle costs are the NAT Gateway and RDS.

```bash
# RDS can be stopped for up to 7 days
aws rds stop-db-instance --db-instance-identifier sih26104-audit

# The NAT Gateway cannot be "stopped" — it is delete/recreate only.
# For a 5-day sprint, leaving it up is the right trade: recreating it churns
# route tables and risks a broken Day-3 deploy for a small saving.
```

---

## 15. Setup completion checklist

| # | Item | Verified by | Status |
|---|---|---|---|
| 1 | Paid Plan + credit balance recorded | `docs/manifests/aws_account_baseline.md` | ⬜ |
| 2 | **`g4dn.xlarge` quota ≥ 4 vCPU** (or increase filed with a request ID) — **Phase 0 DoD blocker** | `service-quotas get-service-quota` | ⬜ |
| 3 | `g4dn.xlarge` confirmed available in the chosen AZs | `describe-instance-type-offerings` | ⬜ |
| 4 | OIDC provider + `gh-actions-deploy-role`, **no `AdministratorAccess`**, **no long-lived keys anywhere** | `iam get-role-policy` | ⬜ |
| 5 | `iam:PassRole` scoped to named roles, not `"*"` | policy JSON review | ⬜ |
| 6 | Repo is `sih26104-voice-integrity`, private; trust policy `sub` matches it | §3.2 + §3.4 | ⬜ |
| 7 | Branch protection on `main`: PR + ≥1 review + `contract-test` + `secret-scan` | `gh api .../branches/main/protection` | ⬜ |
| 8 | CDK bootstrapped in **both** `ap-south-1` **and** `us-east-1` (§4 — `CostSafetyStack` deploys to the latter) | `CDKToolkit` stack exists in each region | ⬜ |
| 9 | **3** ECR repos (`gateway`, `scorer-gpu`, `scorer-cpu`), immutable tags, scan-on-push, lifecycle policy | `ecr describe-repositories` | ⬜ |
| 10 | 4 Secrets Manager entries exist | `secretsmanager list-secrets` | ⬜ |
| 11 | Secrets rotated from placeholders (before first real session) | rotation log | ⬜ |
| 12 | **6 stack files deployed** — 5 chained + standalone `CostSafetyStack`, the latter **before** `SecretsStack` | `cloudformation list-stacks` | ⬜ |
| 13 | **ECS desired=0, ASG 0/0/0** | §7.2 verification block | ⬜ |
| 14 | RDS reachable **only** from Gateway SG (negative test performed) | SG rules + failed connection attempt | ⬜ |
| 15 | Alembic migration applied to RDS | migration task log | ⬜ |
| 16 | Deny-list test passed against **RDS**, not just local Postgres | test task log | ⬜ |
| 17 | CloudFront service-managed SG bound to ALB, **documented** | `docs/manifests/cloudfront_sg_bind.md` | ⬜ |
| 18 | `/api/*` and `/ws/*` behaviors have caching **disabled** | `cloudfront get-distribution-config` | ⬜ |
| 19 | WebSocket headers forwarded on `/ws/*` | origin request policy review | ⬜ |
| 20 | PWA loads over CloudFront | `curl https://$CF_DOMAIN/` | ⬜ |
| 21 | Cognito: SMS disabled, TOTP available, app client has no secret | `describe-user-pool` | ⬜ |
| 22 | Budget with **ACTUAL + FORECASTED** thresholds | `budgets describe-budgets` | ⬜ |
| 23 | **RuntimeStopper tested against a RUNNING GPU** | Lambda invoke + §12 verification | ⬜ |
| 24 | `deploy-runtime` and `stop-runtime` workflows each run successfully ≥ twice | Actions run logs | ⬜ |
| 25 | Scorer banner confirms `CUDAExecutionProvider` | CloudWatch log grep | ⬜ |
| 26 | Image digests (not tags) recorded in `docs/manifests/release_manifest.json` | file diff | ⬜ |

Rows 2, 13, 17, 23, and 25 are the ones that silently fail and ruin Day 3. Verify them with the
commands, not from memory. Rows 2, 6, and 7 are Phase 0 Definition-of-Done items
([phases.md](phases.md) §1.4) — they gate Day 1 for the whole team, not just Pair A.
