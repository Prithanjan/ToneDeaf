# PRD — Voice Integrity Control Plane (SIH26104 / PS104)

**Status:** Authoritative product requirements for the five-day build.
**Derived from:** `PS104_Final_Architecture_Blueprint.md`, `PS104_AI_Training_and_Evaluation_Playbook.md`, `PS104_Five_Day_Implementation_Plan.md`, `SIH26104_Phase1-3_Implementation_Runbook.md`, `research-evidence.md`, `Part-2(Claude Scoped).pdf` (added 2026-08-26).
**Companion docs:** [README.md](README.md) · [architecture.md](architecture.md) · [technical-design.md](technical-design.md) · [design.md](design.md) · [phases.md](phases.md) · [rules.md](rules.md) · [memory.md](memory.md) · [aws-setup-instructions.md](aws-setup-instructions.md)

---

## 0. Source-fidelity notice (read this first)

The blueprint references three authoritative inputs **that are not present in the supplied document set**:

| Referenced input | Cited as | Present? |
|---|---|---|
| SIH26104 problem statement (verbatim) | `pasted_content_2.txt` | ❌ path-only reference |
| Expected Outcome & Privacy Layer report | `SIH26104—Expected-OutcomeAlignmentandPrivacy-LayerReport.md` | ❌ path-only reference |
| Binding CPU-only fallback specification | `pasted_content_3.txt` | ⚠️ only summarized, in `research-evidence.md` §"Binding CPU-Only Local Fallback Specification" |

Consequence: the `PS-01…PS-11` and `OUT-01…OUT-04` requirement IDs that
`PS104_Five_Day_Implementation_Plan.md` §2 mandates for the traceability matrix are
**reconstructed** in §8 below from blueprint §1's one-sentence restatement of the problem
statement. They are labelled `DERIVED`. **Action required from the team:** obtain the two
missing source files and reconcile §8 against them before the traceability matrix is shown to
judges. Do not present a derived ID as a quoted requirement.

### 0.1 New normative source — `Part-2(Claude Scoped)`, added 2026-08-26

A repo → GitHub Actions → AWS execution plan arrived after the six root documents were written. It is
**present in full** and is now the source for a set of decisions that were previously *ours* — filling
gaps the earlier documents left open rather than contradicting them. The markings below are upgraded
accordingly:

| Item | Was | Now |
|---|---|---|
| Phase 0 existing as a numbered phase at all | our inference (both source plans assumed it silently) | **sourced** — [phases.md](phases.md) §1 |
| CI/CD story: GitHub Actions, OIDC-to-AWS, no long-lived keys, one workflow per service, promotion by image digest | our design (sources had none) | **sourced** — [architecture.md](architecture.md) §4.3, [rules.md](rules.md) R-55…R-57 |
| CDK decomposition: 5 dependency-ordered stacks + standalone `CostSafetyStack` | our decomposition | **sourced** — [architecture.md](architecture.md) §4.1 (with one internal conflict reconciled; see `H-5` in §9.1) |
| Repository name `sih26104-voice-integrity`, private, OIDC scoped to `repo:<org>/sih26104-voice-integrity:*` | unspecified | **sourced and pinned** |
| Three ECR repositories (`gateway`, `scorer-gpu`, `scorer-cpu`) | our inference from the parity exception | **sourced** — [rules.md](rules.md) R-06 |
| Team split into Pair A / B / C with these exact ownerships, and the two-key `contracts/` review | our allocation | **sourced** — §9 below, [rules.md](rules.md) R-22 |
| Secrets-Manager-placeholders-before-task-definitions ordering | our inference | **sourced** — [aws-setup-instructions.md](aws-setup-instructions.md) §6 |
| Local Compose tier built Day 1 in parallel as the integration harness; AWS proven on Day 3 | our sequencing | **sourced** — [architecture.md](architecture.md) §5 |
| GPU quota filed in **Phase 0**, not Day 5 | our escalation | **sourced and strengthened** — a Phase 0 DoD blocker, `H-2` |
| `docs/manifests/` as the path for `aws_account_baseline.md`, `release_manifest.json`, `cloudfront_sg_bind.md` | our layout | **sourced** |
| `ScriptProcessor` capture in Phase 1–3; AudioWorklet is Future Scope | blueprint backlog item | **sourced and made explicit** |

**What it does *not* change:** the new PDF does not contain the two missing source files, does not
restate the problem statement verbatim, and does not mention `PS-*`/`OUT-*` IDs. **Every `DERIVED`
marking in §8 therefore stands unchanged.** The PDF also scopes itself to Phase 0–3 and does not
supersede Phases 4–5 ([phases.md](phases.md) §0).

---

## 1. Product definition and non-goals

> **One-line definition (verbatim from blueprint):** Voice Integrity Control Plane is a
> privacy-preserving, real-time decision layer that turns persistent evidence of synthetic or
> manipulated speech into a proportionate verification control before a simulated high-risk
> voice action is completed.

The central engineering principle — and the single most important thing a judge must
understand — is the **separation of detection from decision**:

- The **Scorer** answers one narrow question: *how much evidence of synthetic/manipulated
  speech is present in this 2.56-second voiced window?*
- The **Gateway** answers a different question: *given recent evidence, declared purpose,
  policy version, and uncertainty, what is the appropriate safe next action?*

A score is an observation. An action is a decision. No model output ever calls a banking API.

### 1.1 Explicit non-goals

These are not scope reductions to be quietly restored later. They are claim boundaries.

| The product is NOT | Because |
|---|---|
| A universal "deepfake truth machine" | Benchmark performance on ASVspoof does not generalize to unseen generators, Indian languages, mobile mics, or VoIP codecs |
| A biometric identity or speaker-verification system | The model classifies bona-fide vs spoof at window level; it does not identify persons. Cross-session speaker comparison is **off by default** |
| A fraud-detection or fraud-reduction claim | No operational pilot exists. "Simulated prevention-control effectiveness" is the only supportable outcome claim |
| An automated denial system | The policy vocabulary contains `continue`, `verify`, `hold`, `escalate`. It does **not** contain `approve` or `deny` — see [rules.md](rules.md) R-07 |
| A carrier-grade real-time service | No SLA is claimed before load and network testing |
| An anonymization guarantee | HMAC pseudonymization of a call reference does not make all associated metadata anonymous |

---

## 2. Users and primary scenario

| Actor | Need | Surface |
|---|---|---|
| Bank/telecom fraud analyst (primary) | See accumulating voice-integrity evidence during a high-risk voice interaction and receive a proportionate control action | React PWA on a mobile handset |
| Judge / evaluator (five-day) | Verify the claim end-to-end, including the privacy boundary, on both deployment tiers | Same PWA + read-only audit query + Privacy Inspector |
| Downstream workflow system (simulated) | Receive a feature-only risk event and hold a release | Signed webhook (Phase 4 target) |
| Platform operator | Deploy, stop, and prove cost containment | GitHub Actions `deploy-runtime` / `stop-runtime`, AWS console |

### 2.1 Primary scenario — high-risk action lifecycle

1. Analyst signs in (Cognito SRP on AWS; restricted local JWKS test issuer on the CPU tier).
2. Analyst selects a consented demonstration context, e.g. `payment_release`.
3. **Before microphone access**, the PWA displays a purpose-and-privacy notice.
4. PWA `POST /api/v1/sessions` with the human-readable `client_call_ref`. Gateway immediately
   computes a server-keyed HMAC and returns `session_id` + opaque `call_ref`. The raw reference
   never leaves Gateway memory.
5. PWA `POST /api/v1/stream-ticket` → 60-second signed ticket (keeps the bearer token out of
   the WSS query string).
6. PWA opens `wss://<edge>/ws/v1/stream` with subprotocols `sih-v1` + the ticket.
7. First WSS message is `session.open` carrying **only** opaque `call_ref`, `purpose_code`,
   `context_value_band`.
8. Every subsequent binary frame: `uint64 big-endian sequence` + `640 bytes` of 16 kHz mono
   PCM16 (little-endian samples) = exactly **648 bytes**. See [technical-design.md](technical-design.md) §2.
9. Gateway runs VAD, retains only voiced samples in a **2.56-second in-process ring buffer**,
   and requests a score at **640 ms hops**.
10. Gateway → Scorer gRPC `ScoreWindow` (81,920-byte window). Scorer returns calibrated
    `spoof_risk`, model version, calibration version, quality flags.
11. Gateway applies temporal evidence: **≥3 of the 5 most recent eligible windows high** →
    state transitions `collecting`/`uncertain` → `high`.
12. Policy maps `(purpose_code, risk_state)` → action: `hold` for payment release / beneficiary
    change, `verify` for account recovery, `escalate` for support.
13. Gateway writes a **feature-only** audit event, hash-chained to its predecessor.
14. On disconnect, the PCM ring buffer is cleared. Nothing audio-derived is persisted.

---

## 3. Functional requirements

### FR-1 Ingress and session binding
- FR-1.1 `POST /api/v1/sessions` requires a valid bearer JWT, accepts `client_call_ref`,
  `purpose_code`, `context_value_band`; returns `session_id` and HMAC `call_ref`.
- FR-1.2 `POST /api/v1/stream-ticket` requires a valid bearer JWT and `session_id`; returns a
  ticket valid **60 seconds**, single-use.
- FR-1.3 `WSS /ws/v1/stream` MUST reject: missing/invalid/expired/replayed ticket, disallowed
  `Origin`, non-monotonic or duplicate sequence, frame length ≠ 648 bytes, `session.open`
  whose `purpose_code` does not match the server-side session record, oversized text frames.
- FR-1.4 A session may bind exactly one live WSS connection at a time.

### FR-2 Audio handling
- FR-2.1 Only voiced samples (per WebRTC VAD) accumulate toward a scoring window.
- FR-2.2 Window = 2.56 s = 40,960 samples = 81,920 bytes. Hop = 640 ms = 10,240 samples.
- FR-2.3 No score is requested until the ring buffer holds a full voiced window.
- FR-2.4 The ring buffer is process-memory only, is never written to disk, and is zeroed on
  disconnect, error, or session close.
- FR-2.5 Silence is never scored. A VAD outcome is **not** spoof evidence.

### FR-3 Scoring
- FR-3.1 gRPC `VoiceScorer.ScoreWindow` accepts exactly 81,920 bytes with `contract_id =
  "raw-waveform-v1"` and `sample_rate_hz = 16000`; any other shape is rejected, not coerced.
- FR-3.2 Response carries calibrated `spoof_risk ∈ [0,1]`, `model_version`,
  `calibration_version`, `quality_flags`, and an `eligible` boolean.
- FR-3.3 Scorer exposes model/calibration SHA-256 and the active ONNX execution provider on a
  health endpoint and in its startup banner.
- FR-3.4 Mock mode exists for transport testing only, is named
  `MOCK_SMOKE_MODE_NOT_A_DETECTOR`, is stamped into every response and audit row, and **refuses
  to start** when the release manifest asserts `policy_eligible`.

### FR-4 Policy and decision
- FR-4.1 Risk states: `collecting`, `uncertain`, `high`. No other state exists.
- FR-4.2 Evidence rule: **k-of-n = 3-of-5** over the most recent *eligible* windows only.
- FR-4.3 Actions: `continue`, `verify`, `hold`, `escalate`. `approve` and `deny` are absent from
  the enum by construction.
- FR-4.4 A single high window MUST NOT produce a high-risk action.
- FR-4.5 The purpose→action map is versioned in `policy/policy.yaml` and its hash is recorded in
  every audit row.
- FR-4.6 Policy consumes a calibrated score through a stable interface (score in → action out),
  so a calibration artifact can be swapped without touching policy code.

### FR-5 Audit and evidence
- FR-5.1 Every decision writes one audit row containing only the allow-listed fields in
  [technical-design.md](technical-design.md) §5.
- FR-5.2 `event_hash = HMAC-SHA256(chain_key, canonical_json(row) ‖ prev_event_hash)`; genesis
  `prev` = 32 zero bytes.
- FR-5.3 A verifier recomputes the chain and fails deterministically on any single-row
  alteration.
- FR-5.4 A retention worker deletes rows past `retention_expires_at`.
- FR-5.5 Every row records `policy_version`, `model_version`, `calibration_version`,
  `execution_provider`, and `deployment_profile` — this is what makes the dual-tier parity claim
  provable from the table itself.

### FR-6 Privacy surface
- FR-6.1 Purpose-and-privacy notice is shown **before** `getUserMedia` is called.
- FR-6.2 Privacy Inspector (Phase 4) shows: raw-audio-off, opaque reference, retention period,
  model/policy version, inference profile, current action explanation.
- FR-6.3 Raw scores are never shown without state + action context.

### FR-7 Deployment and cost
- FR-7.1 AWS tier defaults to **zero** ECS desired count and **zero** GPU ASG desired capacity.
- FR-7.2 Runtime start is a manual `workflow_dispatch` with an explicit `confirm_cost_aware`
  input. No `git push` can start GPU spend.
- FR-7.3 `stop-runtime` runs after **every** session, without exception.
- FR-7.4 Budget → SNS → `RuntimeStopper` Lambda sets both ECS service counts and the ASG
  min/max/desired to zero. A direct EC2 stop is insufficient because the ASG relaunches it. This path
  is a **delayed** control — budgets evaluate against cost data that refreshes at most a few times a
  day, so the alert fires hours after the spend. It is a bounded-loss backstop, not a circuit breaker.
- FR-7.5 The CPU-only local Compose tier runs the same application source, contract, schema,
  policy bundle, model, and calibration artifact, and prints all hashes in its startup banner.
- FR-7.6 CI authenticates to AWS via **GitHub OIDC only**. No long-lived access keys exist anywhere,
  and `gh-actions-deploy-role` carries no `AdministratorAccess`.
- FR-7.7 CI builds and pushes an image on every `main` merge but **never scales runtime**. Promotion
  between environments is by **image digest**, never a rebuild; `deploy-runtime` accepts digests, not
  tags.

---

## 4. Non-functional requirements

Targets are **measurement commitments**, not guarantees. Sourced from blueprint §7.

| ID | NFR | Five-day target | Metric | Non-claim |
|---|---|---|---|---|
| NFR-1 | First decision latency | Measure p50/p95 from voiced-audio start; ≤5 s where model + device support it | `voice_first_decision_latency_ms` | Not an SLA |
| NFR-2 | Score hop completion | Each eligible 640 ms hop completes before the next | `scorer_latency_ms`, `scorer_queue_depth` | Single GPU serializes work |
| NFR-3 | Availability | Rehearsed AWS **and** local fallback | `stream_reconnect_total`, `gateway_healthy` | Single-AZ, single GPU node |
| NFR-4 | Privacy | Zero raw bytes persist by default | `raw_audio_persisted_bytes` = 0, `retention_delete_total` | HMAC ≠ full anonymity |
| NFR-5 | Cost | One GPU max; runtime zero after session | `gpu_runtime_minutes`, `runtime_stop_total` | Budgets are delayed, not a circuit breaker |
| NFR-6 | Audit integrity | HMAC chain + retention worker | `audit_hash_verification_failures` = 0 | Root-checkpoint signing is Phase 4+ |
| NFR-7 | Backpressure | Gateway **refuses** a new high-risk stream rather than queue unbounded audio | `stream_rejected_backpressure_total` | — |

---

## 5. Model requirements (from the ML playbook)

> **Core ML rule (verbatim):** A model is eligible for the live policy only when its provenance,
> input/output contract, calibration, benchmark results, codec/language/generator hold-outs, and
> ONNX parity checks are all recorded in a release manifest.

| Requirement | Decision |
|---|---|
| Primary model | **AASIST** (raw-waveform spectro-temporal graph attention), official PyTorch reference |
| Comparators (must run) | **LFCC-LCNN** and **RawNet2** — AASIST must match or exceed both before anything else proceeds (Baseline gate) |
| Rejected for five days | Ensemble/fusion, SSL-encoder candidate, CQCC-GMM as live detector |
| Ablation-gated | CQT, phase, bicoherence, prosody diagnostics — advisory only until ablation proves incremental value without fairness regression |
| Calibration | Platt scaling fit on `dev_calibration` **only**, frozen with the checkpoint; isotonic compared only if it improves ECE without locked-set regression |
| Export contract | `[1, 40960]` float32 raw normalized mono waveform; PCM16→float conversion documented **outside** the graph; output class orientation explicit |
| Splits | `train`, `dev_calibration`, `eval_locked` (+ `eval_generator_heldout`, `eval_codec_language_heldout`, `demo`), grouped **before** augmentation, disjoint by speaker / parent sample / session / generator family+version |
| Datasets | ASVspoof 2019 LA (train/dev), ASVspoof 2021 LA+DF (report separately), MLAAD (pinned revision, generator-disjoint), IndicVoices (accepted terms, bona-fide only), team consented local set |
| Forbidden | Using an 8 kHz/16 kHz sampling boundary as spoof evidence; labelling natural speech as spoof; tuning on `eval_locked` |
| Artifact states | `research_only` → `demo_eligible` → `policy_eligible`. Only `policy_eligible` may drive a high-risk action |

### 5.1 Evaluation gates (all are release blockers)

| Gate | Pass condition | Owner | Failure response |
|---|---|---|---|
| Data | Manifest validated; no split leakage; licence/consent present | Data lead | Stop training, repair provenance |
| Baseline | AASIST ≥ LFCC-LCNN and RawNet2 on declared dev protocol | ML lead | Investigate input pipeline before adding features |
| OOD | Generator-, codec-, language-, device-held-out results reported | ML + eval lead | Restrict claim or retain `uncertain`; never hide the gap |
| Calibration | Improved ECE/Brier on dev without harmful locked-set regression | ML lead | Freeze simpler calibrator or retrain |
| ONNX parity | Output ranking + calibrated decisions match reference within predeclared tolerance | ML + platform lead | **Block deployment artifact** |
| Quantization | Locked-set metrics, calibration, temporal policy remain acceptable | ML lead | Retain FP32 |
| Privacy | No raw audio/transcript/embedding in audit or log export | Privacy lead | **Block demo release** |
| Demo | AWS GPU and local CPU run the same test trace with recorded latency | Team lead | Fix parity or present one tier only, truthfully |

---

## 6. Privacy and threat requirements

Deny-by-default. The browser cannot reach Gateway, Scorer, or the database directly.

| Threat | Enforced control | Required test |
|---|---|---|
| Raw audio in a bucket, DB, log, crash dump, or alert | No audio object store; no audio DB columns; redacting structured logger; volatile buffer clear; payload-size guards | Schema + log scan asserting raw-byte count is zero |
| Replayed or malformed stream | JWT, short-lived single-use signed ticket, Origin allowlist, monotonic sequence, exact 20 ms framing | WSS negative-contract tests (missing ticket, wrong Origin, duplicate sequence, wrong byte length) |
| Cross-tenant disclosure | *Target:* tenant claim, PostgreSQL RLS, tenant-scoped HMAC/encryption context | Integration test returns 403 / zero rows for wrong tenant |
| Overconfident model action | 3-of-5 evidence, `uncertain` state, human verification instead of denial | Adversarial noisy/codec sample must not auto-approve or auto-deny |
| Secrets in source or client | Secrets Manager / Docker secret injection, no client secret, CI secret scan | CI secret scan + deployment manifest inspection |
| Audit record alteration | HMAC event chain (+ Phase 4 signed root checkpoint) | Alter one historical row in a test copy → verifier fails deterministically |

Framing references: NIST Privacy Framework (privacy risk management as repeatable practice);
OWASP WebSocket Security Cheat Sheet (authentication + origin validation for WS endpoints).

---

## 7. Release criteria (go / no-go)

| Criterion | Go threshold | No-go / scope reduction |
|---|---|---|
| Model provenance | Manifest + permitted dataset/consent records complete | Demo transport/policy flow only; **do not claim detection** |
| Class semantics | Bona-fide/spoof output orientation verified | Block risk threshold and high-action policy |
| Calibration | ECE/Brier reported, artifact matched to model SHA | Show raw research result only; **no probability language** |
| ONNX parity | Fixed vectors + policy decisions match within tolerance | Do not deploy the ONNX model |
| Local CPU | Measured p95 on the actual host meets chosen cadence, or is honestly slower but stable | AWS only if reliable; otherwise a fixed non-live trace **labelled as recorded** |
| AWS runtime | GPU task healthy, WSS path valid, stop function works | Use local fallback as primary; do not wait for cloud repair |
| Privacy | Zero default raw persistence confirmed | **Stop demo release until fixed** |
| Claims | Traceability + non-claim language approved | Remove any unsupported outcome statement |

Every release carries a manifest: source commit, image digest, model SHA-256, calibration
SHA-256, dataset manifest IDs, evaluation report ID, policy version, API schema hash, deployment
profile (`aws-gpu` | `local-cpu`). **A release without this manifest is not judge-ready and not
reproducible.**

---

## 8. Traceability matrix (DERIVED — reconcile against the missing source files)

Requirement IDs reconstructed from blueprint §1. Every row needs a test, an evidence artifact,
an owner, and an explicit non-claim before Day 4 (per five-day plan §6).

| ID | Requirement (derived) | Where satisfied | Test / evidence | Owner | Non-claim |
|---|---|---|---|---|---|
| PS-01 `DERIVED` | Near-real-time detection of cloned/synthetic speech | Scorer + 2.56 s/640 ms window loop | `voice_first_decision_latency_ms`, latency report | Pair B + A | Not carrier-grade; not all generators |
| PS-02 `DERIVED` | Telephony / VoIP / collaboration channel coverage | Codec round-trip augmentation (Opus, AAC, G.711 µ/A-law); codec-held-out eval | Cohort metrics table | Pair B | Simulated codecs, not live carrier integration |
| PS-03 `DERIVED` | Dynamic risk scoring | Calibrated `spoof_risk` + quality flags | Brier/ECE, reliability diagram | Pair B | A score is not proof of fraud |
| PS-04 `DERIVED` | Contextual / purpose-aware policy | `policy.yaml` purpose→action map, 3-of-5 rule | Deterministic score-sequence unit tests | Pair C | Not an automated decision authority |
| PS-05 `DERIVED` | Alerts to downstream systems | Signed feature-only webhook (Phase 4 target) | Webhook contract test | Pair C | No audio/transcript/human ref in payload |
| PS-06 `DERIVED` | Reusable APIs | `contracts/openapi.yaml`, `contracts/voice_scorer.proto` | Contract compatibility test in CI | Pair A | gRPC is internal-only |
| PS-07 `DERIVED` | Multilingual Indian readiness | IndicVoices bona-fide + MLAAD multilingual spoof; language cohort report | Worst-group metric + max gap | Pair B | Not proof of Indian synthetic-telephony robustness |
| PS-08 `DERIVED` | Minimal-retention privacy | Feature-only schema, volatile ring buffer, retention worker | Deny-list test, log scan, retention deletion test | Pair C | HMAC pseudonym ≠ anonymity |
| PS-09 `DERIVED` | Acoustic/spectral, phase, prosody analysis | Diagnostic sidecar, ablation-gated, advisory only | Ablation report | Pair B | Cannot influence `hold`/`verify` pre-ablation |
| PS-10 `DERIVED` | Audit trail with tamper evidence | HMAC hash chain + verifier | Tamper-detection test | Pair C | No external immutable store yet |
| PS-11 `DERIVED` | Operational resilience (cloud + edge) | AWS GPU tier + CPU Compose tier, rehearsed failover | Day-5 failover recording, matching hashes | Pair A + C | Tunnel is not "offline"; images not byte-identical |
| OUT-01 `DERIVED` | Consented stream → calibrated risk signal | Consent notice → session → WSS → score | Recorded session + calibration artifact | Pair C | — |
| OUT-02 `DERIVED` | Temporal evidence accumulation | 3-of-5 eligible-window rule | Risk timeline in PWA + audit rows | Pair C | — |
| OUT-03 `DERIVED` | Simulated high-risk workflow control | Mock `hold` before mock payment release | Recorded demo + audit row | Pair C | No real money movement |
| OUT-04 `DERIVED` | No raw audio stored by default | Schema/log scans, Privacy Inspector | Read-only DB query screenshot | Pair C | — |

---

## 9. Team, ownership, and open human decisions

Three pairs, six people, running in parallel with the named sync points in
[phases.md](phases.md) §7. Ownership is now sourced (§0.1), not allocated by us.

| Pair | Owns | Cannot defer past |
|---|---|---|
| **A — Platform/Infra** | `contracts/` (2-key review), `gateway/`, `infra/cdk/`, `infra/compose/` (jointly with C), `.github/workflows/` | Day 2 functional WSS path |
| **B — AI/ML** | `datasets/manifest/`, `ml/`, `evaluation/reports/`, `policy/` artifacts (Pair A consumes), `scorer/` serving | Day 4 deployment candidate |
| **C — Integration & Audit** | `pwa/`, `audit/`, privacy tests, `docs/manifests/release_manifest.json`, demo evidence | Day 4 end-to-end rehearsal |

### 9.1 Blocking human decisions (must close before Day 1)

| # | Decision | Status | Consequence if unresolved |
|---|---|---|---|
| H-1 | **Tie-breaker** for the two-key `contracts/` review when Pair B and Pair C disagree | ⛔ **OPEN — Phase 0 DoD blocker** — nominate a name in `contracts/OWNERS.md` | A contract dispute stalls all three pairs simultaneously, with no way to route around it |
| H-2 | **`g4dn.xlarge` quota in `ap-south-1`** — is the increase filed? | ⛔ **OPEN — Phase 0 DoD blocker** — file in Phase 0, not Day 3 | Quota increases can exceed three days. Nothing in Phase 3 works without it, and the ASG fails to launch *silently*. The 2026-08-26 PDF calls this "the single most avoidable failure mode in this whole plan" |
| H-3 | AWS Paid Plan credit balance recorded in `docs/manifests/aws_account_baseline.md` | ⛔ **OPEN** | No judge-facing cost story; no baseline for the Budget threshold |
| H-4 | Demo laptop identity for the CPU p95 sweep (exact model/CPU) | ⛔ **OPEN** | A p95 from a different host is not a portability promise |
| H-5 | **`CostSafetyStack` deploy position** — confirm the reconciled reading | ⛔ **OPEN** — we deploy it immediately after `DataStack` | The 2026-08-26 PDF says three inconsistent things (file listing: "anytime after data-stack"; prose: "immediately after DataStack"; command listing: after `ComputeStack`). We took the prose reading because the other two permit a window where GPU capacity is deployable with no budget backstop armed. A human confirms or overrides before Phase 2 deploys. See [architecture.md](architecture.md) §4.1 |

H-1 and H-2 are the two the source PDF itself flags as genuinely undecided. They are the only entries
here that are fatal rather than merely late — see [phases.md](phases.md) §1.4.

---

## 10. What "done" means on Day 5

> On Day 5 the team can run the same consented 90-second scenario on **either** AWS or local
> CPU, produce the **same** policy state transition and privacy evidence, and state exactly what
> the model has and has not been validated to prove.

The 90-second judge script is in [phases.md](phases.md) §7.2.
