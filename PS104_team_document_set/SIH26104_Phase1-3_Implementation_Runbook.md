# SIH26104 — Phase 1–3 Implementation Runbook

**Scope:** Repo → GitHub Actions → AWS, for the three sequential phases the two source documents already mandate (contract/privacy → benchmark/calibration → realtime policy/mobile flow). Days 4–5 (dual-tier rehearsal, presentation evidence) and all "target"/backlog items from the blueprint are captured as **Future Scope**, not built now.

**Team split (6 people, 3 pairs):**
- **Pair A — Platform/Infra** (owns Gateway, AWS, CI/CD, contract repo hygiene)
- **Pair B — AI/ML** (owns dataset, training, calibration, ONNX export)
- **Pair C — Integration & Audit** (owns PWA↔Gateway↔Scorer wiring, privacy/audit tests, demo evidence, judge-facing manifest)

Each phase below is split into per-pair tracks that run **in parallel**, with named sync points. This is the fix for gap #5 (contract ownership) and #4 (no team-level Definition of Done): every phase ends with a DoD table before the next phase opens.

---

## 0. Decisions Made to Close the Documented Gaps

| Gap found in source docs | Decision |
|---|---|
| No CI/CD story | GitHub Actions with OIDC-to-AWS (no long-lived keys), one workflow per service, environment promotion via image digest, not rebuild |
| No CDK stack decomposition | 5 stacks, strict dependency order: `NetworkStack → DataStack → SecretsStack → ComputeStack → EdgeStack`, plus a standalone `CostSafetyStack` |
| No IAM/account bootstrap order | Documented in Phase 0 below — must exist before any pair writes application code that assumes it |
| No contract ownership | Pair A owns `contracts/` (protobuf + OpenAPI) as a matter of repo policy; any change requires a PR review from one Pair B and one Pair C member — a lightweight two-key rule since the contract is the seam all three pairs integrate against |
| Local-first vs AWS-first sequencing undefined | **Local CPU Compose tier is built Day 1, in parallel with AWS bootstrap.** It becomes the integration harness Pairs B and C use while Pair A provisions AWS. AWS deploy is proven against the same contract on Day 3, not Day 1. |
| Secrets bootstrap order undefined | Pair A creates all Secrets Manager entries (with placeholder values) as part of Phase 0, before any ECS task definition references them |
| CloudFront gRPC/WebSocket claims are dated | As of ~May 2026, CloudFront added native gRPC support and native WebSocket-over-VPC-origin support. This does **not** change Phase 1–3 architecture — Gateway↔Scorer gRPC stays inside the VPC either way — but it removes a future constraint. Logged under Future Scope, not acted on now. |

---

## 1. Phase 0 — Bootstrap (before Day 1 work starts; ~half a day, Pair A leads, others observe/approve)

This phase doesn't appear as a numbered phase in the source docs, but both docs assume it silently. Skipping it is why "Phase 1" tasks stall on missing AWS permissions.

### 1.1 Repository setup

```bash
# Pair A, once
gh repo create sih26104-voice-integrity --private --clone
cd sih26104-voice-integrity

mkdir -p gateway scorer pwa contracts infra/cdk infra/compose \
         datasets/manifest evaluation/reports policy audit \
         .github/workflows docs/manifests

git checkout -b main
```

Repo layout convention (owners in parentheses):

```
contracts/           # protobuf + OpenAPI — Pair A owns, 2-key review rule
gateway/              # FastAPI app — Pair A
scorer/               # AASIST ONNX serving — Pair B
pwa/                  # React PWA — Pair C
infra/cdk/            # CDK TypeScript, 5 stacks — Pair A
infra/compose/        # local CPU fallback — Pair A + Pair C jointly
datasets/manifest/    # manifest.parquet, split hashes — Pair B
evaluation/reports/   # gate reports, metric tables — Pair B
policy/               # policy.yaml, calibration.json — Pair B → Pair A consumes
audit/                # schema, hash-chain verifier, retention worker — Pair C
docs/manifests/        # release_manifest.json per build — Pair C owns, all pairs contribute fields
.github/workflows/    # CI — Pair A owns
```

Branch protection on `main`: require PR, require 1 review minimum, require the `contract-test` and `secret-scan` CI checks to pass. Set this up now — retrofitting it after three pairs have pushed directly to `main` is a bad Tuesday.

### 1.2 AWS account and IAM bootstrap (Pair A)

1. Confirm the AWS **Paid Plan** and note the credit balance (blueprint constraint table, row "Cost"). Screenshot/record it in `docs/manifests/aws_account_baseline.md` — you'll want this for the judge-facing cost story.
2. Set the default region: `ap-south-1`.
3. Create a dedicated deployment IAM role for CI, **not** a human user's keys:
   ```bash
   # OIDC provider for GitHub Actions
   aws iam create-open-id-connect-provider \
     --url https://token.actions.githubusercontent.com \
     --client-id-list sts.amazonaws.com \
     --thumbprint-list <github-oidc-thumbprint>
   ```
   Then a role `gh-actions-deploy-role` trusted only for `repo:<org>/sih26104-voice-integrity:*`, scoped to: ECR push, ECS update-service, CDK deploy permissions (CloudFormation, IAM PassRole restricted to the specific execution roles, S3 for CDK assets), Secrets Manager read for verification only. **No `AdministratorAccess`.** This closes gap #3.
4. Bootstrap CDK once: `cdk bootstrap aws://<account-id>/ap-south-1`.
5. Create the ECR repositories now (empty is fine): `sih26104/gateway`, `sih26104/scorer-gpu`, `sih26104/scorer-cpu`.
6. Create Secrets Manager placeholder entries so later stacks can reference ARNs without ordering problems:
   ```bash
   for name in sih26104/db-password sih26104/ticket-signing-key \
               sih26104/hmac-key sih26104/audit-chain-key; do
     aws secretsmanager create-secret --name "$name" \
       --secret-string "CHANGE_ME_$(openssl rand -hex 16)"
   done
   ```
   Rotate these to real generated values before any real demo session — placeholders exist only to unblock IaC wiring.

### 1.3 Contract skeleton (Pair A drafts, Pair B + Pair C review same day)

Before any service code, land:
- `contracts/voice_scorer.proto` — the `ScoreWindow` RPC, exact 81,920-byte / 16kHz / `raw-waveform-v1` input, `spoof_risk` + model/calibration version + flags output. This is the seam between Pair A's Gateway and Pair B's Scorer — get the byte contract right before either side writes serving code.
- `contracts/openapi.yaml` — `/api/v1/sessions`, `/api/v1/stream-ticket`, `/ws/v1/stream` message schemas (`SessionOpen`, `risk.event`, error events). This is the seam for Pair C's PWA.
- `contracts/CONTRACT_CHANGE_POLICY.md` — one paragraph: any change requires a version bump, a compatibility note, and the two-key review from §0.

**DoD for Phase 0:** repo exists with branch protection; CI OIDC role deploys a "hello world" CDK stack successfully; all three contract files exist and are merged; every pair can `git clone` and run `docker compose up` on an empty skeleton without errors.

---

## 2. Phase 1 (Day 1) — Contract and Privacy Boundary

Source docs mandate this comes first: "contract and privacy boundary first" (blueprint §8), and ML playbook Day 1 = "dataset manifest, consent ledger, ASVspoof baseline environment, model contract test vector." Below is how that plays out per pair with actual AWS/CI steps.

### 2.1 Pair A — Gateway skeleton + local Compose fallback + CI pipelines

**GitHub Actions — build/push/deploy pattern (write once, reuse per service):**

```yaml
# .github/workflows/gateway-ci.yml
name: gateway-ci
on:
  push:
    paths: ["gateway/**", "contracts/**"]
    branches: [main]
  pull_request:
    paths: ["gateway/**", "contracts/**"]

permissions:
  id-token: write   # required for OIDC
  contents: read

jobs:
  test-and-build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Contract test (protobuf + OpenAPI compatibility)
        run: ./scripts/contract_test.sh
      - name: Secret scan
        uses: trufflesecurity/trufflehog@main
        with: { path: ./gateway }
      - name: Unit + WSS negative-contract tests
        run: |
          cd gateway && pip install -r requirements.txt --break-system-packages
          pytest -q
      - name: Configure AWS credentials (OIDC)
        if: github.ref == 'refs/heads/main'
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::<account-id>:role/gh-actions-deploy-role
          aws-region: ap-south-1
      - name: Build & push image (main only)
        if: github.ref == 'refs/heads/main'
        run: |
          aws ecr get-login-password | docker login --username AWS \
            --password-stdin <account-id>.dkr.ecr.ap-south-1.amazonaws.com
          IMAGE_TAG=$(git rev-parse --short HEAD)
          docker build -t <account-id>.dkr.ecr.ap-south-1.amazonaws.com/sih26104/gateway:$IMAGE_TAG gateway/
          docker push <account-id>.dkr.ecr.ap-south-1.amazonaws.com/sih26104/gateway:$IMAGE_TAG
          echo "IMAGE_DIGEST=$(docker inspect --format='{{index .RepoDigests 0}}' <account-id>.dkr.ecr.ap-south-1.amazonaws.com/sih26104/gateway:$IMAGE_TAG)" >> $GITHUB_ENV
```

Key rule (closes gap #1 properly): **CI builds and pushes an image on every `main` merge, but nothing auto-deploys to ECS in Phase 1.** ECS `desired count = 0` per the blueprint's cost posture. Deployment is a manual, explicit `workflow_dispatch` job added in Phase 3 once there's something worth running on GPU. Building this way from day one means Phase 3 is "flip a switch," not "write the pipeline under time pressure."

**Local Compose fallback (build in parallel, same day):**

```yaml
# infra/compose/docker-compose.yml — skeleton, services expand each phase
services:
  caddy:
    image: caddy:2
    ports: ["443:443"]
    volumes: ["./Caddyfile:/etc/caddy/Caddyfile", "./certs:/certs"]
  gateway:
    build: ../../gateway
    expose: ["8080"]
    environment: { DATABASE_URL: postgresql://..., EXECUTION_PROVIDER: cpu }
  scorer:
    build: ../../scorer
    expose: ["50051"]
  db:
    image: postgres:16
    volumes: ["pgdata:/var/lib/postgresql/data"]
volumes: { pgdata: {} }
```

Gateway skeleton work for Day 1: `POST /api/v1/sessions` (HMAC pseudonymization of `client_call_ref` — this is a **privacy-boundary** requirement, not a nice-to-have, build it first), `POST /api/v1/stream-ticket`, and the WSS negative-contract tests from the blueprint's threat table (§4): missing ticket, wrong Origin, duplicate sequence, wrong byte length. These tests are your Phase 1 exit criteria, not an afterthought.

### 2.2 Pair B — Dataset manifest, consent ledger, ASVspoof baseline env

No AWS dependency yet — this can start the moment Phase 0 lands.

1. Stand up the training environment (locked `uv`/conda env, CUDA version pinned) — do this on a machine with a GPU if available; it does not need to be AWS yet, since ECS GPU capacity is `desired=0` until Phase 3.
2. Build `datasets/manifest/manifest.parquet` per the mandatory field list (playbook §2.1). Write the loader/validator as a CI-checkable script (`scripts/validate_manifest.py`) so a bad manifest fails fast rather than silently corrupting a split later.
3. Implement the split protocol (playbook §2.2) as code, not a spreadsheet — `train` / `dev_calibration` / `eval_locked` with disjointness enforced by speaker/parent/generator hash, checked before any augmentation runs.
4. Consent ledger for the team's local set: written consent captured, `consent_basis` and `retention_expiry` populated per sample — this is a **Data gate** blocker (playbook §6.1), so it belongs in Phase 1, not deferred.
5. Contract test vector: a fixed 40,960-sample float32 array that both PyTorch and (later) ONNX must score identically — write this now, use it at the parity gate in Phase 2.

**Data gate check-in end of Day 1:** manifest validated, no split leakage, licence/consent present, owned by Pair B, reviewed by Pair C (privacy) — per playbook's own gate table.

### 2.3 Pair C — Audit schema, privacy test harness, PWA capture skeleton

1. Land the PostgreSQL audit schema exactly as bounded in blueprint §6.3: allowed fields only, explicit deny-list test that asserts `BYTEA` audio / transcript / raw phone number / speaker embedding are structurally absent from the schema (not just "we don't plan to write them" — enforce it at the schema level with no such columns existing).
2. Implement the HMAC hash-chain writer + verifier stub (even before real events exist, the chain math should be unit-tested against tamper detection — blueprint §4 threat table, "Audit record alteration").
3. PWA: session creation call, purpose-and-privacy notice screen, `ScriptProcessor`-based PCM capture (per blueprint's honest current-state column — AudioWorklet is Future Scope, don't build it now).
4. Draft `docs/manifests/release_manifest.json` schema (empty/templated) — Pair C owns pulling every pair's hashes into this file starting Phase 2, so the template needs to exist before there's anything to fill in.

### 2.4 Phase 1 Definition of Done (all three pairs sign off before Phase 2 opens)

| Item | Owner | Evidence |
|---|---|---|
| Contract files merged, two-key review policy active | Pair A | PR history on `contracts/` |
| Gateway passes WSS negative-contract tests | Pair A | CI run link |
| CI builds + pushes Gateway image via OIDC, `desired=0` confirmed | Pair A | ECR image digest + `aws ecs describe-services` output |
| Local Compose stack runs end-to-end on a laptop | Pair A + Pair C | Screen recording or terminal log |
| Dataset manifest validated, split disjointness enforced, Data gate passed | Pair B | `evaluation/reports/data_gate.md` |
| Consent ledger complete for local set | Pair B | signed consent records (not raw audio) referenced by ID |
| Audit schema deny-list test passing (no audio/transcript/embedding columns exist) | Pair C | CI test output |
| HMAC hash-chain tamper test passing | Pair C | CI test output |
| PWA can open a session and display the privacy notice | Pair C | demo clip |

If any row is red, **do not start Phase 2 for that track** — the blueprint is explicit that no diagnostic/ensemble/dashboard work may become a primary input before the core loop's gates pass; the same discipline applies to the team schedule.

---

## 3. Phase 2 (Day 2) — Benchmark, Calibration, and Infra Buildout

Source docs: blueprint §8 "benchmark and calibration second"; playbook Day 2 = "AASIST baseline score pipeline plus LFCC-LCNN/RawNet2 comparator... do not add diagnostics/ensembles before baseline works."

### 3.1 Pair A — CDK stack buildout (the 5 stacks, in dependency order)

This is where gap #2 (no stack decomposition) gets resolved concretely.

```
infra/cdk/lib/
  network-stack.ts       # VPC, private subnets, 1 NAT gateway, SGs (deny-by-default)
  data-stack.ts          # RDS PostgreSQL 16 db.t4g.micro, private, encrypted, single-AZ
  secrets-stack.ts       # references the Phase-0 Secrets Manager entries by ARN
  compute-stack.ts       # ECS cluster, GPU ASG (desired=0), Gateway + Scorer task defs, Cloud Map
  edge-stack.ts          # CloudFront, S3+OAC for PWA, VPC-origin ALB binding
  cost-safety-stack.ts   # AWS Budget -> SNS -> RuntimeStopper Lambda (standalone, deploy anytime after data-stack)
```

Deploy order matters because of cross-stack references (VPC ID, Cloud Map namespace, secret ARNs):

```bash
cdk deploy NetworkStack
cdk deploy DataStack --exclusively
cdk deploy SecretsStack --exclusively
cdk deploy ComputeStack --exclusively   # desired count 0 — no cost yet
cdk deploy CostSafetyStack --exclusively
cdk deploy EdgeStack --exclusively      # do this last; CloudFront distribution takes ~15 min to propagate
```

Day 2 deliverable: all 5 stacks deployed with **zero running compute** (ECS desired=0, GPU ASG desired=0), RDS up and migrated (Alembic — noted as recommended in the stack table, adopt it now rather than "ad hoc schema changes"), and the manual CloudFront-service-managed-SG bind documented as a runbook step (`docs/manifests/cloudfront_sg_bind.md`) since blueprint §2 flags this as a manual step, not automatable via CDK today.

Also stand up `CostSafetyStack` immediately after `DataStack` — don't leave it for later, since the whole point is it should exist before anyone flips `deployRuntime=true` in Phase 3.

### 3.2 Pair B — Baseline training, comparator, first calibration pass

1. Train AASIST on `train`, early-stop on `dev_calibration` EER, per the exact starting config in playbook §4 (batch 16, AdamW, LR 1e-4 fine-tune, cosine w/ 5% warmup, seeds 17/23/41 — don't deviate before a working baseline exists).
2. Train/run LFCC-LCNN and RawNet2 as comparators on the same declared protocol — this is the **Baseline gate**: AASIST must match or exceed both before anything else proceeds.
3. Fit Platt scaling on `dev_calibration` only, freeze with the model checkpoint. Report Brier score / ECE on `dev` — this is the **Calibration gate**.
4. Do **not** touch ONNX export, diagnostics, or ensembling yet — playbook's stop condition for Day 2 is explicit.

**End of Day 2 gate check:** Baseline gate + Calibration gate (first pass) recorded in `evaluation/reports/`, each tagged with the seed set and dataset manifest hash.

### 3.3 Pair C — Policy engine wiring + audit event writes + Compose integration test

1. Implement the sequential policy engine against `policy.yaml` (three-of-five high-window rule → `collecting`/`uncertain`/`high`), consuming whatever calibrated score Pair B has as of Day 2 (even a first-pass calibration is enough to wire the state machine — swap the artifact later without changing policy code, since the interface is just "calibrated score in, action out").
2. Wire Gateway → audit write path for real: HMAC call ref, context, score, state, action, policy version, prev/current hash, timestamp — run the full Compose stack (Gateway + Scorer stub + Postgres) and confirm an end-to-end scored session produces a correct, chained audit row with zero raw-audio bytes anywhere (this is where the Phase 1 deny-list test earns its keep — run it against real traffic, not just schema).
3. Start filling `release_manifest.json` fields as they become available (dataset manifest hash from Pair B, contract hash from Pair A) — don't wait until Phase 5 to discover a field nobody tracked.

### 3.4 Phase 2 Definition of Done

| Item | Owner | Evidence |
|---|---|---|
| 5 CDK stacks deployed, zero running compute cost | Pair A | `cdk deploy` logs + `aws ecs describe-services` showing desired=0 |
| RDS reachable only from Gateway SG (verified negative test) | Pair A | SG rule + a failed connection attempt from elsewhere |
| CostSafetyStack live (Budget→SNS→Lambda) even though nothing is running yet | Pair A | Lambda ARN + Budget console screenshot |
| Baseline gate passed (AASIST ≥ LFCC-LCNN & RawNet2 on dev) | Pair B | metric table in `evaluation/reports/` |
| Calibration gate passed (first-pass ECE/Brier acceptable) | Pair B | reliability diagram artifact |
| Policy engine produces correct state transitions on synthetic score sequences | Pair C | unit test suite |
| End-to-end Compose run produces a valid, chained, audio-free audit event | Pair C | CI integration test + manual log inspection |

---

## 4. Phase 3 (Day 3) — Realtime Policy, Mobile Flow, and First AWS Deploy

Source docs: blueprint §8 "realtime policy and mobile flow third"; playbook Day 3 = "calibration, threshold simulation, ONNX export, PyTorch-to-ONNX parity — do not deploy score if class orientation/calibration is uncertain."

This is the phase where AWS actually gets asked to run something, so it's the highest-coordination day — all three pairs have hard dependencies on each other today.

### 4.1 Pair B — ONNX export and parity (blocks everyone else's AWS deploy)

1. Export frozen model to `aasist.onnx` per the exact contract (playbook §7): input `[1, 40960]` float32, PCM16→float conversion documented outside the graph, output orientation explicit.
2. **ONNX parity gate**: run the Phase-1 fixed test vector through PyTorch and ONNX, assert score/ranking match within predeclared tolerance. This gate **blocks deployment** — if it fails, Pair A does not deploy anything to the GPU scorer today, full stop.
3. Freeze `calibration.json` (slope/intercept, calibration data manifest hash, model SHA) alongside the ONNX artifact.
4. Hand Pair A the model + calibration SHA-256 pair the moment parity passes — this is the trigger for the deploy step below, not a fixed calendar time.

### 4.2 Pair A — First real GPU deploy, gated on Pair B's parity pass

```yaml
# .github/workflows/deploy-runtime.yml — manual trigger, not automatic
name: deploy-runtime
on:
  workflow_dispatch:
    inputs:
      gateway_image_digest: { required: true }
      scorer_image_digest: { required: true }
      confirm_cost_aware: { required: true, type: boolean }

jobs:
  deploy:
    if: ${{ inputs.confirm_cost_aware }}
    runs-on: ubuntu-latest
    permissions: { id-token: write, contents: read }
    steps:
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::<account-id>:role/gh-actions-deploy-role
          aws-region: ap-south-1
      - name: Scale ASG and ECS to demo capacity
        run: |
          aws autoscaling update-auto-scaling-group \
            --auto-scaling-group-name scorer-gpu-asg \
            --min-size 1 --max-size 1 --desired-capacity 1
          aws ecs update-service --cluster sih26104 --service gateway \
            --desired-count 1 --force-new-deployment
          aws ecs update-service --cluster sih26104 --service scorer \
            --desired-count 1 --force-new-deployment \
            --task-definition scorer:<rev-with-new-image-digest>
      - name: Wait for scorer health
        run: ./scripts/wait_for_scorer_healthy.sh
```

Manual `workflow_dispatch` with an explicit `confirm_cost_aware` checkbox is intentional — the blueprint's cost posture ("runtime-off-by-default... manual and automated stops") means nobody should be able to accidentally trigger a GPU spend from a routine `git push`. This directly implements the doc's `deployRuntime=true` gate (blueprint §3.2) as an actual CI control rather than a verbal rule.

After the demo/test window: `desired-capacity 0` and `ecs update-service --desired-count 0` — script this as `stop-runtime.yml`, same pattern, and run it every single time without exception. Don't rely solely on the Budget/Lambda safety net for routine stops; treat that as the backstop the blueprint itself calls "delayed cost control, not an instantaneous circuit breaker."

### 4.3 Pair C — Live end-to-end stream against real AWS deploy

1. Point the PWA at the CloudFront distribution (not Compose) once Pair A's deploy step completes.
2. Run a full session: mic capture → WSS frames → Gateway VAD/windowing → gRPC `ScoreWindow` against the **real ONNX model** (first time this has happened against real AWS infra, not stubs) → policy state → audit write.
3. Validate the parity claim end-to-end: same test call sequence produces the same policy trace on AWS GPU as it did on local Compose CPU (even though Compose is still using a stub or CPU-exported model at this point — full CPU/GPU parity is a Day 4–5 rehearsal item per the blueprint's own sequencing, don't pull it forward).
4. Log first-decision latency and score-to-action latency (blueprint's acceptance measures) for this first real run — even a rough number here catches gross problems (e.g., cold-start GPU latency) three days before they'd otherwise surface.

### 4.4 Phase 3 Definition of Done

| Item | Owner | Evidence |
|---|---|---|
| ONNX parity gate passed | Pair B | parity report, tolerance stated |
| Calibration frozen and hashed | Pair B | `calibration.json` + SHA-256 in manifest |
| `deploy-runtime` workflow successfully scales Gateway+Scorer to desired=1 | Pair A | Actions run log |
| `stop-runtime` workflow successfully returns everything to desired=0 | Pair A | Actions run log, run this at least twice today |
| CostSafetyStack confirmed still armed after manual runs | Pair A | Budget/Lambda check |
| Live PWA→CloudFront→Gateway→Scorer(GPU)→Policy→Audit round trip succeeds at least once | Pair C | recorded session + audit row |
| First-decision / score-to-action latency numbers logged (no target claimed yet) | Pair C | raw numbers in `evaluation/reports/latency_day3.md` |

---

## 5. Cross-Cutting: How the Three Tracks Actually Sync

Given three pairs working in parallel, put these on the calendar now rather than discovering the need for them mid-week:

- **End of Day 1:** 15-min contract freeze check — has anything in `contracts/` changed since morning? If yes, all three pairs re-pull before starting Day 2 work.
- **End of Day 2:** Pair B hands Pair C the first-pass `policy.yaml` + calibration artifact hash even though ONNX export isn't done — Pair C's policy-engine wiring only needs the *interface* (score in, action out), not the final model.
- **Mid Day 3:** hard blocking sync — Pair A does not run `deploy-runtime` until Pair B confirms the ONNX parity gate in writing (a Slack message pointing at the CI report is enough; the point is it's not assumed).
- **Every phase end:** whoever owns `docs/manifests/release_manifest.json` (Pair C) collects that day's hashes from the other two pairs before end of day, not retroactively.

---

## 6. Future Scope (explicitly not Phase 1–3 — logged so nobody "accidentally" builds it early)

Carried forward from the blueprint's own "target addition" list, plus the CloudFront update found during validation:

| Item | Why deferred |
|---|---|
| EventBridge Scheduler nightly stop | Budget→SNS→Lambda already covers the Phase 1–3 cost-safety requirement; add the second stop path once the demo cadence is known (Day 4–5) |
| mTLS Gateway↔Scorer | VPC isolation + SGs is the documented five-day-adequate control; mTLS is a production-hardening item |
| Cognito Authorization Code + PKCE | SRP is the honest MVP path per the blueprint; swap before any production/enterprise claim, not before the demo |
| AudioWorklet capture (replacing `ScriptProcessor`) | Explicit backlog item in the blueprint; test Safari/Android Chromium behavior before committing engineering time |
| Tenant claim + PostgreSQL RLS + encryption context | Current schema is intentionally single-tenant for the demo |
| Diagnostics plane (CQT/phase/bicoherence/prosody) as a policy input | Ablation-gated by the playbook itself — build the sidecar only after the core loop's gates all pass, and even then it stays advisory-only until ablation proves incremental value without fairness regression |
| Native CloudFront gRPC / WebSocket-over-VPC-origin (newly available) | Doesn't change the Phase 1–3 topology (internal gRPC stays internal); worth a design-review spike in Phase 4+ to see if it simplifies the edge stack, not worth destabilizing a working Day-3 deploy to adopt mid-sprint |
| Quantization, ensembling, SSL-encoder candidate | All explicitly deferred/rejected in the playbook's model-landscape table for the five-day window |

---

## 7. What Still Needs a Human Decision Before Day 1

The docs are unusually rigorous, but two things are genuinely undecided and will block a pair if left ambiguous on the morning of Day 1:

1. **Who is the tie-breaker on the two-key contract review** if Pair B and Pair C disagree on a `contracts/` change? Nominate one person now.
2. **What's the actual GPU quota status in `ap-south-1`** for `g4dn.xlarge`? The blueprint lists this as a Day-5 risk ("Confirm quota and actual image/model start on AWS") but if the quota request hasn't been filed, file it in Phase 0 — AWS quota increases can take longer than three days, and finding out on Day 3 that you can't launch the instance is the single most avoidable failure mode in this whole plan.
