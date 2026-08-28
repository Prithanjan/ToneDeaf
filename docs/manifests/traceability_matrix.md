# Requirement → Evidence Traceability Matrix
## ToneDeaf Voice Integrity Control Plane — SIH26104 Phase 4

> **Status**: Phase 4 complete. All acceptance criteria below are evidenced by verifiable
> artifacts at the committed SHA `aa3f3b41355431cd942d3371276733905c7496bc`.
> Updated: 2026-08-28

---

## How to Read This Matrix

| Column | Meaning |
|--------|---------|
| **Req ID** | PS104 requirement or internal rules.md invariant |
| **Requirement** | What must be true |
| **Evidence Type** | `test` / `ci` / `manifest` / `policy` / `code` |
| **Evidence Pointer** | File + line range or CI run ID |
| **Status** | ✅ Met / ⚠️ Partial / ❌ Not met |

---

## 1. Model Integrity

| Req ID | Requirement | Evidence Type | Evidence Pointer | Status |
|--------|-------------|---------------|-----------------|--------|
| R-42 | Model weights never committed to git | policy | `.gitignore` L15: `*.onnx` | ✅ |
| R-45 | Wrong execution provider causes fail-fast | code | `scorer/app/model.py` — ORT provider guard | ✅ |
| R-51 | Calibration fixture gitignored | policy | `.gitignore` — `*.npy` excluded | ✅ |
| R-56 | ECR images promoted by digest only | manifest | [`release_manifest.json`](release_manifest.json) §docker_images | ✅ |
| PS104-M1 | AASIST model SHA verified at startup | code | `scorer/app/model.py` SHA env check; SHA `45d6eeef…` in manifest | ✅ |
| PS104-M2 | Score inversion contract documented | manifest | [`release_manifest.json`](release_manifest.json) §score_contract | ✅ |
| PS104-M3 | Platt calibration parameters frozen | manifest | `calibration_fixture_v1` SHA `008a60f3…` | ✅ |

---

## 2. Privacy & Data Handling

| Req ID | Requirement | Evidence Type | Evidence Pointer | Status |
|--------|-------------|---------------|-----------------|--------|
| R-14 | No raw audio stored in database | test | `scripts/verify_database_privacy.py --self-test` 6/6 checks ✅ | ✅ |
| R-21 | Audio samples have retention expiry | manifest | All 80 `vc_robustness.manifest.json` records: `retention_expiry: 2028-08-28` | ✅ |
| R-38 | Augmented sample shares split with parent | manifest | `validate_manifest.py` D-09 check — PASS; grouping_key verified | ✅ |
| PS104-P1 | Privacy Inspector UI available to judge | code | [`pwa/src/components/PrivacyInspector.tsx`](../../pwa/src/components/PrivacyInspector.tsx) — 528 lines, 4 tabs | ✅ |
| PS104-P2 | HMAC session pseudonymization | code | `PrivacyInspector.tsx` §HMACPseudonymizationProof tab | ✅ |
| PS104-P3 | Cryptographic hash transparency panel | code | `PrivacyInspector.tsx` §ArtifactSHA256ParityGrid tab | ✅ |
| PS104-P4 | Audit chain verifiable by judge | code | `PrivacyInspector.tsx` §SessionAuditChainVerifier tab | ✅ |
| PS104-P5 | Zero raw audio in RDS confirmed | test | `verify_database_privacy.py` check #1: `COUNT(*) WHERE audio IS NOT NULL = 0` | ✅ |

---

## 3. Robustness Evaluation

| Req ID | Requirement | Evidence Type | Evidence Pointer | Status |
|--------|-------------|---------------|-----------------|--------|
| PS104-R1 | Codec boundary handling — GSM 8k | test | `test_adversarial_stress.py::TestCodecsPipelineStress::test_short_frame_boundaries[gsm_8k-*]` — all pass | ✅ |
| PS104-R2 | Codec boundary handling — Opus 24k | test | `test_adversarial_stress.py::TestCodecsPipelineStress::test_short_frame_boundaries[opus_24k-*]` — all pass | ✅ |
| PS104-R3 | Codec boundary handling — G.711 A-law | test | `test_adversarial_stress.py::TestCodecsPipelineStress::test_short_frame_boundaries[g711_alaw-*]` — all pass | ✅ |
| PS104-R4 | RVC v2 spoofs detectable | manifest | `vc_robustness.manifest.json` §eval_generator_heldout: rvc records with label=spoof | ✅ |
| PS104-R5 | SO-VITS-SVC 4.0 spoofs in calibration set | manifest | `vc_robustness.manifest.json` §dev_calibration: so-vits-svc generator family | ✅ |
| PS104-R6 | Generator family split disjointness | manifest | `validate_manifest.py` D-09 check — PASS: rvc ∩ dev_calibration = ∅ | ✅ |
| PS104-R7 | Sub-frame audio padded safely | code | [`evaluation/codecs.py`](../../evaluation/codecs.py) L97-105 (gsm guard), L261-263 (opus guard) | ✅ |
| PS104-R8 | Robustness test suite 100% green | test | `pytest evaluation/tests/ — 158 passed` | ✅ |
| PS104-R9 | Adversarial stress suite 122 tests | test | `test_adversarial_stress.py — 122 passed` | ✅ |

---

## 4. CI / Deployment Integrity

| Req ID | Requirement | Evidence Type | Evidence Pointer | Status |
|--------|-------------|---------------|-----------------|--------|
| R-37 | Each build promoted by digest | ci | CI run `33179835101`: build-push-scorer → ECR digest verified | ✅ |
| PS104-C1 | scorer-ci passes lint + unit + Docker build | ci | Run `33179835101`: lint ✅ unit ✅ docker-cpu ✅ docker-gpu ✅ | ✅ |
| PS104-C2 | privacy-check job clean | ci | Run `33179835101`: privacy-check ✅ (RELEASE BLOCKER: success) | ✅ |
| PS104-C3 | contract-check job clean | ci | Run `33179835101`: contract-check ✅ | ✅ |
| PS104-C4 | secret-scan job clean | ci | Run `33179835101`: secret-scan ✅ | ✅ |
| PS104-C5 | ORT version consistent across all configs | code | `onnxruntime==1.20.0` across Dockerfile/requirements/compose/ci-yml | ✅ |
| PS104-C6 | `ml/models/` directory tracked in git | code | `ml/models/.gitkeep` committed — Docker COPY now resolves | ✅ |

---

## 5. Audit & Evidence Pack

| Req ID | Requirement | Evidence Type | Evidence Pointer | Status |
|--------|-------------|---------------|-----------------|--------|
| PS104-E1 | Release manifest with frozen hashes | manifest | [`docs/manifests/release_manifest.json`](release_manifest.json) — all SHA-256 verified | ✅ |
| PS104-E2 | Traceability matrix (this document) | manifest | `docs/manifests/traceability_matrix.md` | ✅ |
| PS104-E3 | Validate-manifest script exit=0 on eval set | test | `validate_manifest.py` exit 0 — 0 errors, 2 warnings (eval_locked empty, expected) | ✅ |
| PS104-E4 | Scorer test suite 309 tests green | test | `pytest scorer/tests — 309 passed` | ✅ |
| PS104-E5 | Audit test suite 482 tests green | test | `pytest audit/tests — 482 passed` | ✅ |

---

## 6. Open Items / Known Gaps

| Gap ID | Description | Severity | Remediation |
|--------|-------------|----------|-------------|
| G-01 | `eval_locked` split absent in eval manifest | Warning | Add `eval_locked` split for final numeric EER/minDCF production before PS104 final gate |
| G-02 | `train` split absent in eval manifest | Warning | Synthetic manifest has no training data — not required for Phase 4 eval track |
| G-03 | E2E WSS stream test against live Gateway not yet run | Low | Services at desired=0 for cost hold; run `evaluation/e2e_stream_test.py` after scale-up |
| G-04 | session_id column missing from manifest schema | Conflict M-2 | Playbook §2.1 and §2.2 disagree on field name — blocked on upstream playbook resolution |

---

*Generated by Phase 4 orchestration. For questions contact the ToneDeaf engineering lead.*
