# Rules — Binding Engineering Invariants

**Status:** Binding. These are not style preferences. Each rule exists because a source document
made it a gate, a stop condition, or a claim boundary.
**Audience:** every human and every AI agent working in this repo. **Read this before your first edit.**
**Companions:** [README.md](README.md) · [prd.md](prd.md) · [architecture.md](architecture.md) · [technical-design.md](technical-design.md) · [design.md](design.md) · [phases.md](phases.md) · [aws-setup-instructions.md](aws-setup-instructions.md) · [memory.md](memory.md)

> **How to use this file:** if a change would violate a rule, the change is wrong — not the rule.
> If the rule is genuinely wrong, change *this file* in its own PR with a rationale, then change
> the code. Never silently diverge.

> **Rule IDs are permanent.** They are cited from source-code docstrings across the repo. Never
> renumber an existing rule; new rules continue the sequence. Current range: **R-01 … R-58**.

---

## A. Claim integrity

**R-01 — Never present a target as complete.**
[architecture.md](architecture.md) §3 lists eight target-vs-current deltas (PKCE, AudioWorklet,
mTLS, Scheduler, tenancy, diagnostics, and two edge items). In any document, UI string, commit
message, slide, or verbal demo, an unimplemented item is described as *backlog*, never as present.
*Source: blueprint §2 — "Treat those as explicit backlog items, not hidden equivalence claims."*

**R-02 — Never claim fraud reduction.**
The supportable outcome is *simulated prevention-control effectiveness*. Real fraud reduction
requires an operational pilot. This applies to the pitch, the README, and any metric label.

**R-03 — No capability claim without recorded evidence.**
Every claim maps to a test and an artifact in `evaluation/reports/` or `docs/manifests/`.
*Source: five-day plan §1, invariant four.*

**R-04 — Deployment tier is configuration, never a code branch.**
`if profile == "aws"` in application code is forbidden. Provider, issuer, trust root, storage
backing, and reachability are config values. The Gateway and Scorer application contracts do not
branch on AWS vs local.
*Source: five-day plan §1, invariant one.*

**R-05 — A local no-password JWKS issuer is a test harness, not authentication.**
It must be labelled demo-only in the UI, refuse to start under `DEPLOYMENT_PROFILE=aws-gpu`, and
never be described as RBAC.
*Source: `research-evidence.md`, mandatory correction 3.*

**R-06 — CPU and GPU images are not byte-identical, and nobody says they are.**
One contains `onnxruntime`, the other `onnxruntime-gpu`. The invariant is the parity set in
[architecture.md](architecture.md) §5.1 — not the image bytes. This is why there are **three** ECR
repositories (`sih26104/gateway`, `sih26104/scorer-gpu`, `sih26104/scorer-cpu`) and not two: the
exception is visible in the registry rather than hidden behind a tag convention.
*Source: `research-evidence.md`, mandatory correction 1; ECR set confirmed by the 2026-08-26 PDF.*

---

## B. Decision safety

**R-07 — The action vocabulary is closed: `continue` · `verify` · `hold` · `escalate`.**
`approve` and `deny` must not exist in any enum, config value, database CHECK constraint, or API
schema. This is enforced structurally so that adding one is not a one-line change.
*Source: five-day plan §5 — "Never add a direct 'approve' action or irreversible side effect."*

**R-08 — One high window never triggers a high-risk action.**
The k-of-n rule (3-of-5 eligible windows) is the minimum evidence bar. Lowering `k` to 1 for a
"more responsive demo" is prohibited.
*Source: playbook §5 — "The model should never block an action on one high window."*

**R-09 — Ineligible windows are skipped, never counted as low-risk.**
A codec-degraded or quality-flagged window is absence of evidence, not evidence of absence.

**R-10 — No model output calls an external system directly.**
The Gateway decides; a decision may notify. A score may not.

**R-11 — A score is not calibrated until a calibration artifact says so.**
While `policy/calibration.json` carries `status: placeholder-not-policy-eligible`, no probability
language may be used in UI, logs, or docs, and CI blocks a `policy_eligible` release.
*Source: playbook §5 + go/no-go table.*

**R-12 — Diagnostics are advisory only until the ablation gate passes.**
`policy/diagnostics.py` may compute descriptors; the policy engine **discards** the return value.
Never hard-code a frequency boundary, sampling rate, or spectral cutoff as spoof evidence.
*Source: playbook §3 decision rule, §4.1, §8.*

**R-13 — `high` is sticky within a session.**
Evidence does not evaporate because the next window looked clean. Clearing requires an explicit
human resolution step (Phase 4), never an automatic decay.

---

## C. Privacy boundary

**R-14 — No raw audio persists, anywhere, by default.**
No audio object store. No audio DB column. No audio in a log, crash dump, metric attribute, error
message, or alert payload. The ring buffer is process memory only and is cleared in a `finally`.
*Source: blueprint §4 threat table; five-day plan §1, invariant two.*

**R-15 — The forbidden-column list is structural, not aspirational.**
Columns matching `%audio%`, `%pcm%`, `%waveform%`, `%transcript%`, `%embedding%`, `%phone%`,
`%msisdn%`, `%caller_name%`, `%raw%` must not **exist**. `bytea` is permitted only for the two
32-byte hash columns. The deny-list test asserts the allow-list as an **exact set**, so an
unexpected extra column also fails. See [technical-design.md](technical-design.md) §5.2.

**R-16 — The raw `client_call_ref` lives only in Gateway process memory.**
HMAC is applied before the value can reach a response body, WSS message, log line, gRPC request,
database row, metric label, or webhook payload.

**R-17 — Error messages are static.**
Never interpolate client input into an error string or close reason. That is a documented path for
a caller reference to escape into a log.

**R-18 — Consent precedes capture, in code.**
`getUserMedia` must be structurally unreachable until the purpose-and-privacy notice is
acknowledged. Ordering is a control, not UX polish.

**R-19 — Cross-session speaker comparison is off by default.**
Enabling it requires separate consent, an isolated store, a retention policy, a deletion process,
and a fairness evaluation. No passive embedding retention, ever.

**R-20 — Backpressure means refuse, not queue.**
Gateway refuses a new high-risk stream rather than buffer unbounded audio. Queued audio is
retained audio.

**R-21 — Raw research audio never enters this repository or default cloud storage.**
Manifests carry hashes and IDs. Audio paths stay in controlled research storage. Consented raw
audio is deleted or archived per the **consent ledger**, not the demo DB retention policy.

---

## D. Contract discipline

**R-22 — `contracts/` is owned by Pair A under a two-key rule.**
Any change needs a version bump, a compatibility note, and review from one Pair B **and** one Pair
C member. `contracts/` is the seam all three pairs integrate against, which is why one reviewer is
not enough. The tie-breaker for a B-vs-C deadlock is named in `contracts/OWNERS.md` — that name is
Phase 0 blocker `H-1` ([phases.md](phases.md) §1.4) and is still open. An unnamed tie-breaker stalls
all three pairs at once, with no way to route around it.

**R-23 — Frame and window constants have exactly one definition per language.**
`gateway/app/constants.py` and `pwa/src/lib/constants.ts` are compared by a CI test. Never inline
`648`, `640`, `81920`, or `40960` anywhere else.

**R-24 — Wrong-shaped input is rejected, never coerced.**
A 647-byte frame, an 81,919-byte window, or `sample_rate_hz = 8000` is an error. Padding, trimming,
or resampling to make it fit destroys the contract that makes CPU/GPU parity checkable.

**R-25 — The wire byte order is fixed: sequence `uint64` big-endian; PCM samples `int16`
little-endian.** ([technical-design.md](technical-design.md) D-1, D-2.) Mixed order is deliberate and documented.

**R-26 — Schema changes go through Alembic.** No ad-hoc DDL, on any tier, including local.

**R-27 — Changing the hash-chain field set is a breaking change.**
Bump `CHAIN_FIELD_SET_VERSION` and document the re-anchor. Never quietly add a field to the
canonical serialization — every historical hash becomes unverifiable.

---

**R-58 — Never rotate or re-derive `sih26104/audit-chain-key` once any audit event exists.**
The chain is a keyed HMAC. Rotating the key does not re-key the history — it makes every
previously written `event_hash` unverifiable in one action, with no error at the time and no
recovery afterwards. There is no re-anchoring procedure, because re-anchoring would require
recomputing hashes with the new key, which is indistinguishable from forging them. Consequences
that follow from this and are not separately negotiable: the secret carries
`RemovalPolicy.RETAIN` and must never be added to a `cdk destroy` path
(`infra/cdk/lib/secrets-stack.ts`), and the bulk `put-secret-value` loop in
`aws-setup-instructions.md` §6 must exclude it by name once the first session has been recorded.

> **This rule was added late, and the gap it closed is worth stating.** The prohibition was
> already enforced in code and asserted by an evidence gate, but **no rule of record contained
> it** — five places cited *R-31 (“Stopping means zeroing the ASG too”)* and one cited *R-27
> (the chain **field set**, not the key)*, so a reviewer following any citation landed somewhere
> that did not say this. A control that everything cites and nothing states is one edit away from
> being deleted as unsourced. See `memory.md` §4 BUG-20.

---

## E. Cost and deployment safety

**R-28 — Runtime is zero by default.** ECS desired count 0, GPU ASG desired 0. A fresh deploy costs
nothing to run.

**R-29 — No `git push` can start GPU spend.**
Runtime start is `workflow_dispatch` with an explicit `confirm_cost_aware` input. CI may build and
push images automatically; it may never scale runtime.

**R-30 — Run `stop-runtime` after every session, without exception.**
The Budget → SNS → Lambda path is a **delayed** cost control, not an instantaneous circuit breaker,
and the delay is measured in hours: budgets evaluate against Cost Explorer data that refreshes at most
a few times a day, so the alert fires long after the spend that triggered it. A GPU left running
overnight has already billed for the night. Describe it as a bounded-loss backstop for the case where
a human forgot — never as a circuit breaker, in docs, slides, or code comments. `stop-runtime` is the
mechanism.

**R-31 — Stopping means zeroing the ASG too.**
`min`, `max`, **and** `desired` → 0, plus both ECS service counts. A direct EC2 stop is
insufficient because the ASG relaunches the instance.

**R-32 — One GPU. Ever, for the five-day window.**
Exactly one `g4dn.xlarge` and exactly one scorer GPU allocation. Scale only after latency,
throughput, quota, and policy-state testing demonstrate need.

**R-33 — `CostSafetyStack` deploys before anything can run.**
The CDK decomposition is **five dependency-ordered stacks plus one standalone stack — six files**:
`NetworkStack → DataStack → SecretsStack → ComputeStack → EdgeStack`, and `CostSafetyStack` outside
the chain. Because it depends on nothing, its position is a policy choice, and the policy is:
**deploy it immediately after `DataStack`.** Deploying it after `ComputeStack` leaves a window in
which GPU capacity is deployable with no budget backstop armed — which inverts the control the stack
exists to provide.
*Source: the 2026-08-26 PDF states this three inconsistent ways (see [architecture.md](architecture.md)
§4.1). We took the strictest reading; it is open decision `H-5` until a human confirms it.*

**R-34 — No secrets in Git, images, or the client.**
Secrets Manager on AWS; Docker secrets or a git-ignored `.env` locally. Same logical key names on
both tiers. `secret-scan` is a required check on `main` (R-57). No real account ID, ARN, or secret
value appears in any document in this repository — placeholders must be obviously fake.

**R-35 — Never reload Caddy during a live stream.**
Caddy closes open WebSockets on configuration reload. Reconnect/backoff must be rehearsed.

**R-36 — No SSH, no public IP on the GPU host. No ECS Exec in the demo.**

---

## F. ML governance

**R-37 — Never tune on `eval_locked`.** One final evaluation per candidate release. The locked set
stays untouched.

**R-38 — Group before you augment.**
Split disjointness by speaker, parent sample, session, generator family+version, and text where
feasible — computed **before** any augmentation runs. A derived sample inherits its parent's split.

**R-39 — Sampling rate is a channel characteristic, not spoof evidence.**
Never use an 8 kHz / 16 kHz boundary as a decision rule.

**R-40 — Augmentation applies to both branches.**
Every transformation hits bona-fide and spoof unless it models a separately-labelled attack class.
Otherwise the model learns the augmentation, not the artifact.

**R-41 — Never label natural speech as spoof.** IndicVoices and similar corpora are bona-fide
sources. Accent, illness, emotion, and speaking style are never penalized.

**R-42 — One factor per experiment.**
Every run records: YAML config, Git commit, dataset manifest hash, split hash, seed, environment
lockfile, container digest, Python/driver/CUDA/ORT versions, checkpoint SHA-256, ONNX SHA-256,
calibration SHA-256, the full metric table, and an artifact state
(`research_only` | `demo_eligible` | `policy_eligible`).

**R-43 — Seeds `17`, `23`, `41`; report mean and range.** Never a single lucky seed.

**R-44 — Benchmark metrics and product metrics are reported separately, never averaged.**
ASVspoof 2019 / 2021-LA / 2021-DF are reported separately — never collapsed into one "accuracy".

**R-45 — A silent CPU fallback on the GPU tier is a failure, not a degradation.**
The Scorer asserts the requested execution provider is actually active and exits if not. A silent
fallback invalidates every latency number recorded that day.

**R-46 — Mock mode is loud.**
`MOCK_SMOKE_MODE_NOT_A_DETECTOR` appears in the startup banner, every gRPC response, every audit
row, and the UI. It refuses to start when the release manifest asserts `policy_eligible`.

**R-47 — A measured p95 belongs to a named host.**
Record the exact machine. A p95 from a different laptop is not a portability promise.

**R-48 — Quantization only through the full gate.**
Parity, EER/min-t-DCF, calibration, and temporal-policy regression. INT8 is a separate model
*version*, never a silent artifact replacement.

---

## G. Process

**R-49 — Update [memory.md](memory.md) in the same commit as the change.**
Decisions, deviations, resolved unknowns, and new blockers. This is the handoff mechanism between
humans and between agents. A change that alters a documented decision without updating
`memory.md` is incomplete.

**R-50 — A red Definition-of-Done row blocks the next phase for that track.**

**R-51 — No release without a manifest.**
Source commit, image digest, model SHA-256, calibration SHA-256, dataset manifest IDs, evaluation
report ID, policy version, API schema hash, deployment profile. Without it, a release is not
judge-ready and not reproducible.

**R-52 — Log what you dropped.**
If coverage is bounded (top-N, sampling, skipped cohort, unrun test), say so explicitly. Silent
truncation reads as "covered everything."

**R-53 — Purity is a testability contract.**
`frames.py`, `ring.py`, `engine.py`, `chain.py` (and their PWA counterparts) stay pure and
side-effect-free. No file, socket, DB handle, clock read, or random source inside them — inject
those. These four are where a privacy or correctness bug is most expensive.

**R-54 — When a source document and the implementation disagree, name the difference.**
Do not silently blend them. State the conflict and set the required target decision — that is the
method the blueprint itself uses. The same applies when two source documents disagree, or when one
source disagrees with itself: record which reading was taken, why, and who must confirm it.

---

## H. Delivery pipeline (repo → GitHub Actions → AWS)

Added 2026-08-26. Source: `Part-2(Claude Scoped).pdf`, which closed the "no CI/CD story" gap the
earlier documents left open. The repository is `sih26104-voice-integrity`, **private** — the name is
load-bearing, because the OIDC trust policy matches `sub` against
`repo:<org>/sih26104-voice-integrity:*`. Renaming the repo breaks CI's ability to reach AWS at all.

**R-55 — CI reaches AWS through OIDC only. No long-lived keys, ever.**
GitHub Actions assumes `gh-actions-deploy-role` via `sts:AssumeRoleWithWebIdentity` with
`permissions: id-token: write`. No access-key pair belonging to a human or a machine user is stored
in repository secrets, in an image, or on a laptop that deploys. The role carries **no
`AdministratorAccess`**: it is scoped to ECR push, ECS `update-service`/`describe-*`, Auto Scaling
`update-auto-scaling-group`/`describe-*`, CloudFormation + S3 for CDK, `iam:PassRole` **restricted to
named execution roles**, and `secretsmanager:GetSecretValue` for verification only. A leaked
long-lived key grants standing account access; an OIDC token is minted per run and expires.
*A `PassRole` on `"*"` converts any workflow compromise into account takeover — check that resource
list with your own eyes.*

**R-56 — Promotion is by image digest, never a rebuild.**
`deploy-runtime` takes `gateway_image_digest` and `scorer_image_digest`, not tags. ECR repositories
are `IMMUTABLE`. Workflows are **one per service** (`gateway-ci.yml`, `scorer-ci.yml`,
`pwa-ci.yml`), path-filtered on `<service>/**` + `contracts/**`, so an unrelated change cannot
re-push an image and invalidate a digest a release manifest already recorded. A rebuild from the same
source produces different bytes; the moment a manifest names a tag instead of a digest, it stops
describing what is running (R-51).

**R-57 — `main` is protected from Phase 0, not from Phase 4.**
Require a pull request, require **≥1 approving review**, and require the `contract-test` and
`secret-scan` checks to pass. `privacy-tests` is required on top of the source minimum, because a
privacy regression must not be mergeable. `contracts/` additionally needs the two-key review (R-22).
Retrofitting protection after three pairs have pushed directly to `main` means either rewriting
history or accepting an unreviewed change to the seam all three pairs integrate against.

---

## Quick reference — the seven that are hardest to undo

| | Rule |
|---|---|
| 1 | **R-14** No raw audio persists, anywhere, by default |
| 2 | **R-07** `approve` / `deny` do not exist in the action vocabulary |
| 3 | **R-29** No `git push` can start GPU spend |
| 4 | **R-24** Wrong-shaped input is rejected, never coerced |
| 5 | **R-37** Never tune on `eval_locked` |
| 6 | **R-01** Never present a target as complete |
| 7 | **R-27** Changing the hash-chain field set is a breaking change |
