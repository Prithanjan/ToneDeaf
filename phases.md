# Phases — Execution Plan

**Status:** Authoritative phase plan. Merges the runbook's Phase 0–3 with the five-day plan's
Day 1–5 and the ML playbook's daily priorities into one sequence with one set of gates.
**Companions:** [README.md](README.md) · [prd.md](prd.md) · [architecture.md](architecture.md) · [technical-design.md](technical-design.md) · [design.md](design.md) · [rules.md](rules.md) · [aws-setup-instructions.md](aws-setup-instructions.md) · [memory.md](memory.md)

> **Normative source added 2026-08-26:** `Part-2(Claude Scoped).pdf` — a repo → GitHub Actions → AWS
> execution plan with per-phase Definition-of-Done tables and per-pair track breakdowns. It is now
> the source for Phase 0's existence, the pair split, the CDK decomposition (§3.1), the CI/CD
> posture, and the named sync points (§7). Its DoD tables are **merged** into §1.4 / §2.4 / §3.4 /
> §4.4 below, not appended. Where its DoD is *narrower* than what the earlier documents already
> demand, the stricter requirement is kept and the difference is named in place
> ([rules.md](rules.md) R-54).

---

## 0. Phase map and the sequence that is not negotiable

```
Phase 0  Bootstrap            ~½ day   Pair A leads, others approve
Phase 1  Contract & Privacy   Day 1    ← contract and privacy boundary FIRST
Phase 2  Benchmark & Calib    Day 2    ← benchmark and calibration SECOND
Phase 3  Realtime & Deploy    Day 3    ← realtime policy and mobile flow THIRD
Phase 4  Robustness & Evidence Day 4   ← hardening, Privacy Inspector, evidence pack
Phase 5  Rehearsal & Failover Day 5    ← dual-tier rehearsals and presentation LAST
```

> **Scope note on the 2026-08-26 PDF.** That document scopes itself to **Phase 0–3 only** and files
> Days 4–5 under its own Future Scope. It does **not** supersede Phases 4 and 5 here — those come from
> `PS104_Five_Day_Implementation_Plan.md`, which is still authoritative for the five-day window. Read
> the PDF as the authority on *how Phase 0–3 executes*, and the five-day plan as the authority on
> *what Days 4–5 must produce*. Phases 4 and 5 below are unchanged by this reconciliation.

**Governance rule (blueprint §8):** no diagnostic feature, cross-session identity function, or
visual dashboard feature may become a primary decision input before the core score, calibration,
and policy control loop pass their acceptance gates. *The same discipline applies to the team
schedule:* if a Definition-of-Done row is red, that track does not open the next phase.

**Three pairs, running in parallel with named sync points:**

| Pair | Scope | Owns in repo |
|---|---|---|
| **A — Platform/Infra** | Gateway, AWS, CI/CD, contract repo hygiene | `contracts/` (**2-key review**), `gateway/`, `infra/cdk/`, `infra/compose/` (**jointly with C**), `.github/workflows/` |
| **B — AI/ML** | Dataset, training, calibration, ONNX export | `datasets/manifest/`, `ml/`, `evaluation/reports/`, `policy/` (**produces**; Pair A consumes), `scorer/` |
| **C — Integration & Audit** | PWA↔Gateway↔Scorer wiring, privacy/audit tests, demo evidence, judge-facing manifest | `pwa/`, `audit/`, `docs/manifests/` (**owns `release_manifest.json`**; all pairs contribute fields) |

`contracts/` is the seam all three pairs integrate against, which is why it carries the **two-key
rule**: any change needs a version bump, a compatibility note, and review from one Pair B **and** one
Pair C member ([rules.md](rules.md) R-22). The tie-breaker for a B-vs-C deadlock is `H-1` and is still
open — see §1.4.

---

## 1. Phase 0 — Bootstrap  ⟨status: repo scaffold DONE, AWS actions OPEN⟩

This phase appears in neither source plan as a numbered phase, but both assume it silently.
Skipping it is why "Phase 1" tasks stall on missing AWS permissions.

### 1.1 Repository setup — ✅ done in this commit

Repository name is pinned: **`sih26104-voice-integrity`, created private.** The name is not
cosmetic — the OIDC trust policy in [aws-setup-instructions.md](aws-setup-instructions.md) §3.2
matches `sub` against `repo:<org>/sih26104-voice-integrity:*`, so renaming the repo breaks CI's
ability to assume the deploy role.

Directory layout with owners is live (see [README.md](README.md)). Remaining:

```bash
gh repo create sih26104-voice-integrity --private --source=. --remote=origin --push
```

**Branch protection on `main` — set this up now.** Retrofitting it after three pairs have pushed
directly to `main` is a bad Tuesday.
- Require PR, minimum 1 review.
- Require checks: `contract-test`, `secret-scan`, `privacy-tests`. (The first two are the PDF's
  minimum; `privacy-tests` is ours and stays — a privacy regression must not be mergeable.)
- Contract two-key rule: any `contracts/` change needs one Pair B **and** one Pair C reviewer.

### 1.2 AWS account and IAM bootstrap — ⛔ open

Full step-by-step in **[aws-setup-instructions.md](aws-setup-instructions.md)**. Summary:

| # | Action | Blocker if skipped |
|---|---|---|
| 0.1 | Confirm Paid Plan + record credit balance in `docs/manifests/aws_account_baseline.md` | No judge-facing cost story |
| 0.2 | Set default region `ap-south-1` | — |
| 0.3 | **File the `g4dn.xlarge` quota increase** | ⛔ **Phase 0 DoD blocker.** Can exceed three days. See §1.4 |
| 0.4 | GitHub OIDC provider + `gh-actions-deploy-role` (**no `AdministratorAccess`**, `iam:PassRole` scoped to named exec roles, Secrets Manager read for verification only) | CI cannot deploy; nobody should use human keys |
| 0.5 | `cdk bootstrap aws://<account-id>/ap-south-1` | CDK deploy fails on a missing asset bucket / `cdk-*` roles |
| 0.6 | Create ECR repos: `sih26104/gateway`, `sih26104/scorer-gpu`, `sih26104/scorer-cpu` | Image push fails. **Three, not two** — the CPU tier needs its own image ([rules.md](rules.md) R-06) |
| 0.7 | Create 4 Secrets Manager placeholder entries | Later stacks cannot reference ARNs — ordering deadlock |

Secrets must exist (with placeholders) **before** any ECS task definition references them.
Rotate to real generated values before any real demo session.

### 1.3 Contract skeleton — ✅ landed in this commit

`contracts/voice_scorer.proto`, `contracts/openapi.yaml`, `contracts/frame_contract.md`,
`contracts/CONTRACT_CHANGE_POLICY.md`, `contracts/OWNERS.md`.

### 1.4 Phase 0 Definition of Done

Rows marked ⛔ **BLOCKER** stop Day 1 for the whole team, not just one track. Everything else stops
only the track that owns it ([rules.md](rules.md) R-50).

| Item | Owner | Evidence | Status |
|---|---|---|---|
| Repo `sih26104-voice-integrity` exists, **private**, with the agreed layout | Pair A | this tree | ✅ |
| Contract files exist and are merged | Pair A | `contracts/` | ✅ |
| Branch protection active (PR + ≥1 review + `contract-test` + `secret-scan`), two-key rule on `contracts/` | Pair A | GitHub settings screenshot | ⛔ |
| CI OIDC role deploys a hello-world CDK stack **with no long-lived keys** | Pair A | Actions run log | ⛔ |
| Three ECR repositories exist (`gateway`, `scorer-gpu`, `scorer-cpu`) | Pair A | `ecr describe-repositories` | ⛔ |
| Four Secrets Manager placeholder entries exist | Pair A | `secretsmanager list-secrets` | ⛔ |
| Every pair can clone and `docker compose up` on the skeleton without errors | all | terminal log | ⛔ needs `pnpm i` + mkcert |
| **H-1** contract tie-breaker named in `contracts/OWNERS.md` | Team lead | file diff | ⛔ **BLOCKER** |
| **H-2** `g4dn.xlarge` quota ≥ 4 vCPU in `ap-south-1`, **or** an increase filed with a request ID | Pair A | Service Quotas request ID in `aws_account_baseline.md` | ⛔ **BLOCKER** |
| **H-3** credit balance recorded | Pair A | `docs/manifests/aws_account_baseline.md` | ⛔ |
| **H-4** demo laptop named for the CPU p95 sweep | Team lead | `memory.md` entry | ⛔ |
| **H-5** `CostSafetyStack` deploy position confirmed (see §3.1) | Team lead | `memory.md` entry | ⛔ |

> **The two BLOCKER rows are the only Phase 0 items that can be fatal rather than merely late.**
>
> **H-1** — an unnamed tie-breaker stalls all three pairs simultaneously, because `contracts/` is the
> single seam they integrate against. There is no way to route around it.
>
> **H-2** — the `g4dn.xlarge` on-demand quota in `ap-south-1` is, in the 2026-08-26 PDF's own words,
> *"the single most avoidable failure mode in this whole plan."* The earlier blueprint files it as a
> **Day-5** risk; that is wrong, and the PDF is now the source: **file the increase request during
> Phase 0.** A brand-new account frequently has a 0-vCPU G-family quota, approval can take longer
> than three days, and **nothing in Phase 3 works without it** — the ASG will simply fail to launch,
> silently, on the highest-coordination day of the build. Mechanics are in
> [aws-setup-instructions.md](aws-setup-instructions.md) §2; that section is deliberately ordered
> *before* the IAM work for this reason. The DoD row is satisfied by a **request ID**, not by an
> approval — filing is what Phase 0 controls.

---

## 2. Phase 1 (Day 1) — Contract and Privacy Boundary

**Theme:** both infrastructure shells exist and all three pairs agree on the exact data contract.
Do **not** begin by training an untracked model or designing dashboard cards.

### 2.1 Pair A — Gateway skeleton, local Compose, CI

1. `POST /api/v1/sessions` with HMAC pseudonymization of `client_call_ref`. **This is a
   privacy-boundary requirement, not a nice-to-have — build it first.**
2. `POST /api/v1/stream-ticket` — 60 s, single-use, bound to `session_id` + `sub`.
3. **WSS negative-contract tests — these are the Phase 1 exit criteria, not nice-to-haves and not an
   afterthought.** Four are named explicitly by the source and are non-negotiable:

   | # | Negative test | Expected | Threat it closes |
   |---|---|---|---|
   | 1 | **Missing ticket** — no `sih-ticket.` subprotocol offered | reject, `AUTH_TICKET_MISSING` / 1008 | Unauthenticated stream |
   | 2 | **Wrong `Origin`** — not on the allow-list | reject, `AUTH_ORIGIN_DENIED` / 1008 | Cross-site WebSocket hijack |
   | 3 | **Duplicate sequence** — non-monotonic `uint64` | reject, `PROTO_SEQUENCE` / 1003 | Replayed stream |
   | 4 | **Wrong byte length** — binary frame ≠ 648 bytes | reject, `PROTO_FRAME_SIZE` / 1003 | Malformed / coerced input ([rules.md](rules.md) R-24) |

   Two more are **ours and stricter**, and they stay exit criteria too: `purpose_code` mismatch
   against the server-side session record (`PROTO_PURPOSE_MISMATCH`, closes the consent-binding gap
   in [technical-design.md](technical-design.md) D-4) and oversized text frames
   (`PROTO_PAYLOAD_TOO_LARGE`). Codes and close reasons: [technical-design.md](technical-design.md)
   §2.5.
4. Local CPU Compose tier: Caddy + PWA + Gateway + Scorer + Postgres + restricted test issuer.
   Generate the local CA and prepare the phone/LAN trust procedure. Built **Day 1 in parallel** with
   the AWS bootstrap — this is the integration harness Pairs B and C use while Pair A provisions AWS.
   AWS is proven against the same contract on **Day 3**, not Day 1.
5. CI: **one workflow per service** (`gateway-ci.yml`, `scorer-ci.yml`, `pwa-ci.yml`), each
   path-filtered on `<service>/**` + `contracts/**`. Each runs `contract-test`, `secret-scan`, and
   unit tests on every PR, then on `main` assumes `gh-actions-deploy-role` **via OIDC** and
   builds+pushes to ECR. Record the resulting **image digest** — promotion between environments is
   by digest, **never** a rebuild ([rules.md](rules.md) R-56).

> **Key CI rule:** CI builds and pushes an image on every `main` merge, but **nothing auto-deploys
> to ECS in Phase 1.** ECS `desired=0` per the cost posture. Deployment is a manual
> `workflow_dispatch` added in Phase 3. Building this way from Day 1 means Phase 3 is "flip a
> switch," not "write the pipeline under time pressure."

**Stop condition:** if local TLS cannot work on the demo phone, choose the tunnel/QR contingency
**now** and rehearse it. Do not discover this on Day 5.

### 2.2 Pair B — Dataset manifest, consent ledger, baseline env

No AWS dependency — starts the moment Phase 0 lands.

1. Locked training env (`uv`/conda, CUDA version pinned). GPU machine if available; ECS GPU stays
   `desired=0` until Phase 3.
2. `datasets/manifest/manifest.parquet` per the mandatory field list. The loader/validator is a
   **CI-checkable script** (`scripts/validate_manifest.py`) so a bad manifest fails fast rather
   than silently corrupting a split later.
3. Split protocol as **code, not a spreadsheet**: `train` / `dev_calibration` / `eval_locked`, with
   disjointness enforced by speaker / parent / generator hash, checked **before** any augmentation.
4. **Consent ledger** for the team's local set: written consent captured, `consent_basis` and
   `retention_expiry` populated per sample. This is a Data-gate blocker — it belongs in Phase 1.
5. **Contract test vector:** a fixed 40,960-sample float32 array that PyTorch and (later) ONNX must
   score identically. Write it now; use it at the Phase 3 parity gate.

**Stop condition:** no audio enters training without source / licence / consent metadata.

**Data gate check-in, end of Day 1:** manifest validated, no split leakage, licence/consent present.
**Owned by Pair B, reviewed by Pair C for privacy** — the owner of a manifest is not a competent
reviewer of its own consent basis.

### 2.3 Pair C — Audit schema, privacy harness, PWA capture skeleton

1. PostgreSQL audit schema exactly as bounded in [technical-design.md](technical-design.md) §5 — allow-listed fields
   only, plus the **structural deny-list test**. Enforce it at the schema level: the forbidden
   columns must not *exist*, not merely be unwritten.
2. HMAC hash-chain writer + verifier. The chain math is unit-tested against tamper detection
   **before** real events exist.
3. PWA: session creation call, purpose-and-privacy notice screen, `ScriptProcessor` PCM capture
   (AudioWorklet is Future Scope — **do not build it now**).
4. `docs/manifests/release_manifest.json` template — Pair C starts collecting hashes in Phase 2,
   so the template must exist before there is anything to fill in.

### 2.4 Phase 1 Definition of Done

All three pairs sign off before Phase 2 opens for any track.

| Item | Owner | Evidence |
|---|---|---|
| Contract files merged, two-key policy active | Pair A | PR history on `contracts/` |
| Gateway passes **all six** WSS negative-contract tests (§2.1) | Pair A | CI run link |
| CI builds + pushes Gateway image via OIDC, `desired=0` confirmed | Pair A | ECR image digest + `aws ecs describe-services` |
| Local Compose stack runs end-to-end on a laptop | Pair A + C | screen recording / terminal log |
| Dataset manifest validated, split disjointness enforced, **Data gate** passed | Pair B | `evaluation/reports/data_gate.md` |
| Consent ledger complete for local set | Pair B | signed consent records **referenced by ID**, not raw audio |
| Audit schema deny-list test passing (no audio / transcript / embedding column *exists*) | Pair C | CI output |
| HMAC chain tamper test passing | Pair C | CI output |
| PWA opens a session and shows the privacy notice | Pair C | demo clip |
| Log-redaction test passing | Pair C | CI output |
| Fixed 40,960-sample contract test vector committed | Pair B | file + hash in `release_manifest.json` |

> **Where this is stricter than the 2026-08-26 PDF, deliberately.** The PDF's Phase 1 DoD table
> omits three rows kept above:
> - **Log-redaction test** — the PDF enforces the privacy boundary at the *schema* level only. A
>   schema deny-list does nothing about a caller reference or PCM buffer reaching a log line, which
>   is the more likely leak path in a build with debug logging on ([rules.md](rules.md) R-14, R-17).
> - **Contract test vector as a Phase 1 artifact** — the PDF lists it as Pair B track work but not as
>   a DoD row. It is the input to the Phase 3 ONNX parity gate that blocks deployment; if it is not
>   frozen and hashed on Day 1 there is nothing to compare against on Day 3.
> - **Six negative-contract tests, not four** — see §2.1.
>
> Nothing in the PDF's table was dropped. If a row is red, **do not start Phase 2 for that track**:
> no diagnostic, ensemble, or dashboard work may become a primary input before the core loop's gates
> pass, and the same discipline applies to the team schedule ([rules.md](rules.md) R-50).

**Day 1 exit gate (five-day plan §3):** `pnpm build`, `pnpm synth`, PWA build, Python compile, and
`docker compose config` all pass. The team can explain, out loud: the difference between
`client_call_ref` and HMAC `call_ref`; between a raw classifier score and a policy action; and
between an AWS Budget and real-time cost control.

---

## 3. Phase 2 (Day 2) — Benchmark, Calibration, Infra Buildout

**Theme:** validate the non-negotiable path before visual polish — consented capture or approved
fixture → WSS → Gateway → gRPC → Scorer → risk event → feature-only audit.

> A mock scorer is permitted **only** to test transport. It is never proof of detection, and it
> carries the label `MOCK_SMOKE_MODE_NOT_A_DETECTOR` in every response and audit row.

### 3.1 Pair A — CDK buildout: five dependency-ordered stacks + one standalone

**Six stack files.** Five are in a strict dependency chain because of cross-stack references (VPC ID,
Cloud Map namespace, secret ARNs). The sixth, `CostSafetyStack`, is **standalone** — it reads nothing
from the other five, so *where* it goes in the sequence is a policy decision, not a technical one.

```
infra/cdk/lib/
  network-stack.ts       # VPC, private subnets, 1 NAT gateway, SGs (deny-by-default)
  data-stack.ts          # RDS PostgreSQL 16 db.t4g.micro, private, encrypted, single-AZ
  secrets-stack.ts       # references the Phase-0 Secrets Manager entries by ARN
  compute-stack.ts       # ECS cluster, GPU ASG (desired=0), Gateway + Scorer task defs, Cloud Map
  edge-stack.ts          # CloudFront, S3+OAC for PWA, VPC-origin ALB binding
  cost-safety-stack.ts   # AWS Budget -> SNS -> RuntimeStopper Lambda   [STANDALONE]
```

Dependency chain: `NetworkStack → DataStack → SecretsStack → ComputeStack → EdgeStack`.

Deploy sequence — `CostSafetyStack` is interleaved **immediately after `DataStack`**:

```bash
cdk deploy NetworkStack
cdk deploy DataStack       --exclusively
cdk deploy CostSafetyStack --exclusively   # ← standalone, but deployed HERE on purpose
cdk deploy SecretsStack    --exclusively
cdk deploy ComputeStack    --exclusively   # desired count 0 — no cost yet
cdk deploy EdgeStack       --exclusively   # LAST — CloudFront takes ~15 min to propagate
```

> **⚠️ Reconciled source conflict — `H-5`, needs human confirmation.** The 2026-08-26 PDF says three
> different things about when `CostSafetyStack` is deployed: its file listing says *"standalone,
> deploy anytime after data-stack"*, its prose says *"stand up CostSafetyStack immediately after
> DataStack — don't leave it for later"*, and its own command listing places it **after**
> `ComputeStack`. All three agree the file count is six and that it has no chain position.
>
> **We took the prose reading: immediately after `DataStack`.** Reason: the entire purpose of the
> cost-safety plane is to be armed *before* anyone can flip `deployRuntime=true`. Deploying it after
> `ComputeStack` leaves a window in which the GPU ASG and both ECS services are deployable with no
> budget backstop — which inverts the control the stack exists to provide. The permissive readings
> are not *wrong* about dependencies; they are silent about the one thing that matters
> ([rules.md](rules.md) R-33). Logged as open decision **H-5** in [prd.md](prd.md) §9.1 and §1.4
> above; a human confirms or overrides before this sequence is run.

**Day 2 deliverables:** all six stacks deployed with **zero running compute** (ECS `desired=0`, GPU
ASG `desired=0`); RDS up and **Alembic-migrated**; the manual CloudFront service-managed-SG bind
written up as **`docs/manifests/cloudfront_sg_bind.md`** (the blueprint flags this as a manual step,
not CDK-automatable today).

> **Alembic timing — not a conflict, an ordering.** The PDF adopts Alembic at Day 2 ("rather than ad
> hoc schema changes"), when RDS first exists. [technical-design.md](technical-design.md) D-8 adopts
> it in **Phase 1**, when the first table is created locally. Both hold: Phase 1 creates the first
> migration against local Postgres, Phase 2 runs `alembic upgrade head` against RDS. The stricter
> Phase-1 reading stands — whichever phase creates the first table must create the first migration,
> or that table is forever un-migrated ([rules.md](rules.md) R-26).

### 3.2 Pair B — baseline training, comparators, first calibration

1. Train AASIST on `train`, early-stop on `dev_calibration` EER, using the exact starting config
   (batch 16 + grad-accum to 64, AdamW, LR `1e-4` fine-tune, cosine with 5 % warmup, seeds
   `17`/`23`/`41`). **Do not deviate before a working baseline exists.**
2. Run **LFCC-LCNN and RawNet2** comparators on the same declared protocol. **Baseline gate:**
   AASIST must match or exceed both before anything else proceeds.
3. Fit **Platt scaling on `dev_calibration` only**; freeze with the checkpoint. Report Brier / ECE.
   **Calibration gate.**
4. **Do not touch** ONNX export, diagnostics, or ensembling yet. The playbook's Day-2 stop
   condition is explicit.

Report mean **and range** across seeds — not a single lucky seed.

### 3.3 Pair C — policy engine, audit writes, Compose integration

1. Implement the sequential policy engine against `policy.yaml` (3-of-5 → `collecting` /
   `uncertain` / `high`), consuming whatever calibrated score Pair B has as of Day 2. A first-pass
   calibration is enough to wire the state machine — the interface is just *score in, action out*,
   so the artifact swaps later without touching policy code.
2. Wire the real Gateway→audit path and run the full Compose stack. Confirm an end-to-end scored
   session produces a correct, chained audit row with **zero raw-audio bytes anywhere**. This is
   where the Phase 1 deny-list test earns its keep — run it against real traffic, not just schema.
3. Start filling `release_manifest.json` as fields become available. Do not wait until Phase 5 to
   discover a field nobody tracked.

### 3.4 Phase 2 Definition of Done

| Item | Owner | Evidence |
|---|---|---|
| All **six** stacks deployed (5 chained + standalone `CostSafetyStack`), zero running compute cost | Pair A | `cdk deploy` logs + `describe-services` desired=0 |
| RDS reachable **only** from Gateway SG (verified negative test) | Pair A | SG rule + a failed connection from elsewhere |
| `CostSafetyStack` live (Budget→SNS→Lambda) even though nothing is running | Pair A | Lambda ARN + Budget console screenshot |
| CloudFront service-managed-SG bind performed **and documented** | Pair A | `docs/manifests/cloudfront_sg_bind.md` with IDs + date |
| **Baseline gate** passed (AASIST ≥ LFCC-LCNN & RawNet2 on dev) | Pair B | metric table in `evaluation/reports/`, tagged with seed set + manifest hash |
| **Calibration gate** passed (first-pass ECE/Brier) | Pair B | reliability diagram artifact |
| Policy engine produces correct transitions on synthetic sequences | Pair C | unit suite |
| End-to-end Compose run → valid, chained, audio-free audit event | Pair C | CI integration test + manual log inspection |
| `release_manifest.json` carries Day-2 hashes (dataset manifest, contract) | Pair C | file diff, collected **same day** (§7) |

**Day 2 exit gate:** the *exact same* WSS binary fixture reaches both the AWS Gateway and the local
Gateway; each writes a feature-only audit row with an opaque HMAC reference. Mock mode remains
labelled transport-only. The evaluator has a baseline table tagged with the data manifest hash.

---

## 4. Phase 3 (Day 3) — Realtime Policy, Mobile Flow, First AWS Deploy

**Theme:** turn a raw model score into a safer control mechanism. This is the day the *problem
solution* is demonstrated — not merely classification. Highest-coordination day: all three pairs
have hard dependencies on each other.

### 4.1 Pair B — ONNX export and parity  ⟨blocks everyone's AWS deploy⟩

1. Export the frozen model to `aasist.onnx`: input `[1, 40960]` float32, PCM16→float conversion
   documented **outside** the graph, output orientation explicit.
2. **ONNX parity gate:** run the Phase-1 fixed test vector through PyTorch and ONNX; assert score
   and ranking match within the predeclared tolerance. **This gate blocks deployment.** If it
   fails, Pair A deploys nothing to the GPU scorer today — full stop.
3. Freeze `calibration.json` (slope/intercept, calibration data manifest hash, model SHA)
   alongside the ONNX artifact.
4. Hand Pair A the model + calibration SHA-256 pair **the moment parity passes**. That handoff —
   not a calendar time — is the trigger for the deploy below.

### 4.2 Pair A — first real GPU deploy, gated on parity

`deploy-runtime.yml` is `workflow_dispatch` **only**, with required inputs `gateway_image_digest`,
`scorer_image_digest`, and a boolean `confirm_cost_aware` that the job itself guards on
(`if: ${{ inputs.confirm_cost_aware }}`). Both image inputs are **digests, not tags** — this is the
same promotion-by-digest rule CI follows from Day 1 ([rules.md](rules.md) R-56), so the thing that
runs on the GPU is provably the thing the release manifest names. The manual gate is intentional:
**nobody should be able to trigger GPU spend from a routine `git push`.** This implements the
blueprint's `deployRuntime=true` gate as an actual CI control rather than a verbal rule.

It scales the ASG to `min=max=desired=1`, forces new deployments of both ECS services, then blocks on
`./scripts/wait_for_scorer_healthy.sh`.

After the window: `stop-runtime.yml`, same pattern, **run every single time without exception** —
`desired-capacity 0` on the ASG *and* `--desired-count 0` on both services ([rules.md](rules.md)
R-31).

> **Do not treat the Budget/Lambda path as the stop mechanism.** It is a **delayed** cost control, not
> an instantaneous circuit breaker, and the difference is measured in hours: AWS Budgets evaluate
> against Cost Explorer data that refreshes at most a few times a day, so an alert fires long after
> the spend that triggered it. A GPU left running overnight has already billed for the night by the
> time the Lambda zeroes it. `stop-runtime` is the mechanism; the Lambda is the backstop for the case
> where a human forgot ([architecture.md](architecture.md) §7.1, [rules.md](rules.md) R-30).

### 4.3 Pair C — live end-to-end against real AWS

1. Point the PWA at CloudFront (not Compose) once Pair A's deploy completes.
2. Full session: mic → WSS → VAD/windowing → gRPC `ScoreWindow` against the **real ONNX model** →
   policy state → audit write.
3. Validate the parity claim end-to-end: the same test sequence produces the same policy trace on
   AWS GPU as on local Compose CPU. *Full CPU/GPU parity is a Day 4–5 rehearsal item — do not pull
   it forward.*
4. Log first-decision and score-to-action latency for this first real run. Even a rough number
   catches gross problems (e.g. cold-start GPU latency) three days before they'd otherwise surface.

### 4.4 Phase 3 Definition of Done

| Item | Owner | Evidence |
|---|---|---|
| **ONNX parity gate** passed | Pair B | parity report with the **predeclared** tolerance stated |
| Calibration frozen and hashed | Pair B | `calibration.json` + SHA-256 in `release_manifest.json` |
| `deploy-runtime` scales Gateway+Scorer to desired=1 | Pair A | Actions run log |
| `stop-runtime` returns everything to desired=0 | Pair A | Actions run log — **run it at least twice today** |
| `CostSafetyStack` confirmed still armed after the manual runs | Pair A | Budget/Lambda check |
| Live PWA→CloudFront→Gateway→Scorer(GPU)→Policy→Audit round trip succeeds at least once | Pair C | recorded session + audit row |
| First-decision / score-to-action latency logged (**no target claimed yet**) | Pair C | raw numbers in `evaluation/reports/latency_day3.md` |

**Day 3 exit gate:** a real evaluated candidate model — **not a mock** — produces calibrated
scores. Three controlled high windows produce a `hold` **only** for a high-value purpose. Both
local and AWS report the *same* model/calibration hash. CPU latency is measured, not assumed.

---

## 5. Phase 4 (Day 4) — Robustness, Privacy Inspector, Evidence Pack

**Theme:** harden the claim. Output is a presentable evidence pack **with limitations visible, not
hidden.**

| Pair | Work | Stop condition |
|---|---|---|
| **B** | Generator-, codec-, language-, device-, duration-held-out subsets. Diagnostic ablation **only if** the primary baseline is stable. Model card + failure examples + declared limitations | If a subgroup degrades materially, **restrict the claim and retain uncertainty** rather than bury the result |
| **C (PWA)** | Privacy Inspector: raw-audio-off, opaque reference, retention period, model/policy version, inference profile, current-action explanation. Accessibility check | Never expose a raw score without state + action context |
| **A** | EventBridge Scheduler stop target **or** document it as not implemented. Confirm Caddy WSS reconnection after a simulated reload | **Do not represent missing scheduler / mTLS / PKCE as complete** |
| **A (security)** | Cross-tenant test where tenancy exists, else mark production-backlog. Test-issuer restriction, secret scan, direct-service-port denial, Origin denial | A local no-password token issuer **cannot** be presented as authentication |
| **C (QA/pitch)** | Traceability matrix ([prd.md](prd.md) §8) + the exact judge narrative | Remove unsupported graphs or metrics from the pitch |

**Day 4 exit gate:** every PS/OUT requirement has **one test and one judge-visible proof**. The
team can open the DB proof showing no raw audio, demonstrate an HMAC pseudonym, show an action
transition, and explain why real fraud-reduction claims await a pilot.

Also on Day 4: ORT CPU **thread sweep on the actual demo laptop** — record p50/p95 per setting to
`evaluation/reports/cpu_benchmark.csv` with host specs. Do not promise fallback speed until an
actual laptop p95 exists. INT8 quantization only through the full parity/calibration/EER/policy
regression gate.

---

## 6. Phase 5 (Day 5) — Rehearsal, Failover, Final Demo

**Not a feature day.** Freeze the manifest, test both profiles, rehearse the fallback before
judges arrive.

| Timebox | Activity | Required evidence |
|---|---|---|
| First rehearsal | AWS end-to-end with a consented legitimate sample and an approved simulated synthetic/replay sample | Screen recording, model/policy version, score/state timeline, mock hold |
| **Failover rehearsal** | Stop AWS runtime intentionally; start local Compose; scan QR from a judge-like device; repeat the same script | `docker compose ps`, phone WSS run, **same model/calibration hash**, local audit row |
| Privacy proof | Query the audit table, verify no raw-audio fields; show Privacy Inspector + retention config | Read-only query screenshot, log-schema scan |
| Cost proof | ECS services and GPU ASG return to zero | ECS/ASG screenshot + RuntimeStopper log |
| Recovery rehearsal | Simulate a dropped WSS and one Caddy reload; PWA reconnects or shows an explicit retry state | UX video, **no duplicate policy action** |
| Final freeze | Tag the commit, export image/model bundle (`docker save`), archive metric + traceability docs | Release manifest signed by track owners |

### 6.1 Pre-staged failover kit (assemble on Day 4, not Day 5)

Pre-generated local QR code · trusted device plan · **preloaded images and model** (`docker save`
bundle, no network pull) · saved local audit fixture · printed one-page script.

### 6.2 The 90-second judge script

1. State the risk: voice cloning creates a social-engineering channel in high-risk voice workflows.
2. Show the purpose notice and consented microphone activation. Explain that raw audio remains
   transient.
3. Start a legitimate sample. The UI shows `collecting`, then a non-blocking continuation.
4. Switch to an approved synthetic/manipulated test sample. The risk timeline accumulates evidence
   **across windows, not one frame**.
5. After the temporal threshold: `high`, and a **simulated `hold`** before a mock payment release.
6. Open Privacy Inspector and the audit record: opaque reference, feature-only data, model/policy
   versions, action, hash chain — **no raw audio**.
7. State the limitation: this demonstrates *simulated prevention-control effectiveness*, not
   measured reduction in real fraud.
8. If AWS fails: state that the same signed release is running on the local edge fallback, then
   execute the already-rehearsed local script.

### 6.3 Teardown and retrospective

Immediately after: stop both ECS services, set ASG min/desired/max to zero, then
`cdk destroy --force` once AWS evidence is no longer needed. Confirm deletion in CloudFormation and
check Cost Explorer later for residual charges.

Locally: `docker compose down`; delete test volumes **only after** archiving approved feature-only
evidence. Delete or securely archive consented raw research audio **according to the consent
ledger** — not the demo database retention policy.

Retrospective records: unsupported model cohorts, observed latency on both tiers, Caddy/device
trust friction, AWS quota/cost friction, privacy-test gaps, and the top three production
investments — mTLS/service identity, tenant RLS/encryption isolation, broader controlled
codec/language/generator evaluation.

---

## 7. Cross-cutting sync points  ⟨put these on the calendar now⟩

Three pairs working in parallel need these scheduled, not discovered mid-week.

| When | Sync | Duration | Why |
|---|---|---|---|
| **End of Day 1** | **Contract freeze check** — has anything in `contracts/` changed since morning? | **15 min** | If yes, all three pairs re-pull **before** starting Day 2 work. A pair that spends Day 2 against a stale `.proto` loses Day 2 |
| **End of Day 2** | **Handoff:** Pair B → Pair C, first-pass `policy.yaml` + calibration artifact hash — *even though ONNX export is not done* | — | Pair C's policy wiring needs only the *interface* (score in, action out), not the final model. Waiting for the real artifact serializes two days that can run in parallel |
| **Mid Day 3** | **Hard blocking sync:** Pair A does not run `deploy-runtime` until Pair B **confirms the ONNX parity gate in writing** | — | A message pointing at the CI parity report is enough. The point is that it is confirmed, not assumed. This is a *blocking* sync — Pair A idles rather than deploys |
| **Every phase end** | Pair C collects that day's hashes from the other two pairs into `docs/manifests/release_manifest.json` — **before end of day, not retroactively** | — | Retroactive hash collection is how manifests end up wrong, and a wrong manifest is worse than a missing one ([rules.md](rules.md) R-51) |

---

## 8. Future Scope — explicitly NOT Phase 1–3

Logged so nobody "accidentally" builds it early.

| Item | Why deferred |
|---|---|
| EventBridge Scheduler nightly stop | Budget→SNS→Lambda already covers Phase 1–3 cost safety; add the second path once demo cadence is known (Day 4–5) |
| mTLS Gateway↔Scorer | VPC isolation + SGs is the documented five-day-adequate control; mTLS is production hardening |
| Cognito Authorization Code + PKCE | SRP is the honest MVP path; swap before any production claim, not before the demo |
| AudioWorklet capture (replacing `ScriptProcessor`) | Explicit blueprint backlog. **The PWA uses `ScriptProcessor` in Phase 1–3 — do not build AudioWorklet now.** Test Safari + Android Chromium behaviour before committing engineering time, and label the current path in the UI ([rules.md](rules.md) R-01) |
| Tenant claim + PostgreSQL RLS + encryption context | Schema is intentionally single-tenant for the demo (`tenant_id` column pre-provisioned so this is a migration, not a rewrite) |
| Diagnostics plane (CQT / phase / bicoherence / prosody) as a policy input | Ablation-gated by the playbook. Build the sidecar only after the core loop's gates pass — and even then it stays advisory-only until ablation proves incremental value without fairness regression |
| **Native CloudFront gRPC** (available ~May 2026) | Doesn't change Phase 1–3 topology: Gateway↔Scorer gRPC stays **inside the VPC** either way. Worth a Phase 4+ design-review spike to see if it simplifies the edge stack; not worth destabilizing a working Day-3 deploy mid-sprint |
| **Native CloudFront WebSocket-over-VPC-origin** (available ~May 2026) | Same reasoning. It removes a *future* constraint, not a current one — the `/ws/*` behaviour with forwarded upgrade headers already works ([aws-setup-instructions.md](aws-setup-instructions.md) §9.2) |
| Quantization, ensembling, SSL-encoder candidate | Explicitly deferred/rejected in the playbook's model-landscape table for the five-day window |
| Signed audit root checkpoint + external immutable store | Chain + verifier is the five-day control |
| OpenTelemetry metrics/traces | CloudWatch structured logs suffice for the demo |
| Webhook retry / DLQ for integration events | Only safe, non-sensitive events may retry; needs idempotency design |
