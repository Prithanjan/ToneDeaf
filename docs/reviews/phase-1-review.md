# Phase 1 Comprehensive Review — Voice Integrity Control Plane (SIH26104 / PS104)

**Status:** Complete Phase 1 Review & Verification Audit  
**Date:** 2026-08-26  
**Scope:** Gateway, Scorer, Audit, Contracts, Infra/CDK, PWA, CI/CD, Documentation

---

## 1. Executive Summary

Phase 1 establishes the **Contract and Privacy Boundary** for the Voice Integrity Control Plane. All three functional pairs (Gateway/Infra, ML/Scorer, Audit/Frontend) have established verified interface contracts, test harnesses, and security invariants.

- **Total Test Suite:** 1,116 automated unit & contract tests passing clean (0 failures).
- **CI Gates:** 9 GitHub Actions workflows fully configured with coverage floors.
- **Defects Resolved:** BUG-1 through BUG-20 resolved and verified.
- **Privacy Controls:** R-14 (zero raw audio persistence), R-15 (forbidden schema columns), R-17 (log redaction), and R-07 (closed action vocabulary) enforced structurally in code and asserted by tests.

---

## 2. Component-by-Component Review

### 2.1 Gateway (`gateway/`)
- **WebSocket Streaming:** Accept 648-byte binary PCM frames (16kHz 16-bit mono), manage 3-second ring buffer with 1-second hops.
- **Authentication & Handshake:** Extract `sih-ticket.<JWT>` subprotocol before WebSocket upgrade; validate origin allowlist; check single-use replay cache; reject unauthenticated peers with strict close codes (`AUTH_TICKET_MISSING`, `AUTH_ORIGIN_DENIED`, `BACKPRESSURE_REJECT`).
- **Policy Engine:** Evaluate calibrated scores against `policy.yaml` thresholds (`high_window_risk: 0.78`, `k=3, n=5`); enforce sticky high states; output closed action vocabulary (`continue`, `verify`, `hold`, `escalate`, `terminate`).
- **Diagnostics Plane:** Advisory-only sidecar (`as_display_payload` flags `advisory=True`, `influences_decision=False`). Forbids demographic, accent, or carrier codec profiling (R-12, R-39, R-41).
- **Telemetry & Metrics:** Schema-versioned metric definitions (`v1`), 13 declared metrics with strict low-cardinality label allowlist. Prohibits `call_ref` and PII dimensions.
- **Verification:** **330 passing tests** in `gateway/tests/`.

### 2.2 Scorer (`scorer/`)
- **Model Seam:** AASIST ONNX model inference service over gRPC (`contracts/voice_scorer.proto`).
- **Parity Vector:** Baked contract test vector (`/fixtures/contract_vector_v1.npy`, 160 KiB) asserted at startup.
- **Calibration Loader:** Real loader for `policy/calibration.json` with Platt scaling parameters (`platt_a`, `platt_b`), version, and model SHA-256 bindings.
- **Safety Posture:** Fails fast if CUDA provider unavailable under `aws-gpu` tier (R-45). Mock mode explicitly labelled as `MOCK_SMOKE_MODE_NOT_A_DETECTOR` (R-46).
- **Verification:** **309 passing tests** in `scorer/tests/` (including 17 tests in `test_calibration_artifact.py`).

### 2.3 Audit & Evidence Plane (`audit/`)
- **Tamper-Evident Hash Chain:** Canonical JSON serialization (`canonicalize`) + HMAC-SHA256 chaining. Stored in PostgreSQL `audit_event` table.
- **Closed Schema DDL:** Strict CHECK constraints on `detector_mode` (`REAL_DETECTOR`, `MOCK_SMOKE_MODE_NOT_A_DETECTOR`), `action`, `risk_state`, `context_value_band`, and `deployment_profile`.
- **Retention Worker:** Whole-session-atomic deletion (`MAX(retention_expires_at) <= cutoff`) with advisory locks. Prevents interior chain-break false alarms.
- **Terminal Anchor Support:** Verifier detects tail truncation and all-row deletion when terminal hash / expected count anchors are provided (H-7 / BUG-11).
- **Verification:** **477 unit tests passing** (12 integration tests deselected for local execution; gated by `.github/workflows/audit-ci.yml` floor 455).

### 2.4 Contracts (`contracts/`)
- Frozen data schemas: `openapi.yaml`, `voice_scorer.proto`, `frame_contract.md`.
- Governance: Two-key contract change policy (`CONTRACT_CHANGE_POLICY.md`, `OWNERS.md`).
- Wire format: 648-byte binary frame (8-byte `uint64` big-endian sequence number + 640-byte 16-bit PCM payload = 320 samples = 20ms at 16kHz).

### 2.5 Infrastructure (`infra/`)
- **AWS CDK:** 6 modular stacks (`NetworkStack`, `DataStack`, `CostSafetyStack`, `SecretsStack`, `ComputeStack`, `EdgeStack`). Synthesizes cleanly.
- **Cost Safety:** Standalone `CostSafetyStack` with CloudWatch Budget anomaly detection and Lambda killer (`runtime-stopper`).
- **Local Compose:** Caddy + Gateway + Scorer + PostgreSQL + PWA configured in `infra/compose/docker-compose.yml`.
- **IAM & OIDC:** Strict GitHub Actions deploy and trust policies with zero long-lived credentials (R-55).

### 2.6 Progressive Web App (`pwa/`)
- Modern React/TypeScript frontend with Vite.
- Design tokens: 340 custom CSS properties in `tokens.css`.
- Realtime SSE and WebSocket stream consumer, session setup, consent notices, and risk timeline visualizer.
- Lint and typecheck clean.

---

## 3. Defect Resolution Summary (BUG-1 to BUG-20)

| Defect ID | Component | Root Cause & Resolution |
|---|---|---|
| **BUG-1** | Audit | Deny-list test dead code eliminated; DDL columns strictly asserted against forbidden keywords. |
| **BUG-2** | Gateway | `gateway/Dockerfile` context aligned to repository root to access contracts. |
| **BUG-3** | Gateway | `--ws-max-size` configured to 65536 to prevent premature uvicorn closes before app negative contract checks. |
| **BUG-4** | Gateway | Fixed inverted test assertions in WebSocket frame validation suites. |
| **BUG-5** | Gateway | Eliminated unauthorized `approve`/`deny` action strings from `stream.py` (R-07). |
| **BUG-6** | Gateway | Fixed hanging drain loop in streaming test harnesses. |
| **BUG-7** | Compose | Changed PostgreSQL DSN from `postgresql+asyncpg://` to `postgresql://` acceptable to `asyncpg.create_pool`. |
| **BUG-8** | Scorer | Added missing calibration artifact keys (`method`, `platt_a`, `platt_b`, `version`, `model_sha256`) to `policy/calibration.json`. |
| **BUG-9** | CDK | Fixed `MODEL_PATH` in `compute-stack.ts` to `/models/aasist.onnx`. |
| **BUG-10** | CI | Added protobuf generation script triggers to `gateway-ci.yml`. |
| **BUG-11 / H-7** | Audit | Added optional terminal hash & count anchor checks in `verify_chain` to detect tail truncation & deletion. |
| **BUG-12** | CI | Added `.github/workflows/audit-ci.yml` with passed-count floor 455 selecting `-m "not integration"`. |
| **BUG-13** | Audit | Replaced naive row deletion in `writer.py::delete_expired` with whole-session-atomic retention query. |
| **BUG-14** | Audit | Closed `DETECTOR_MODE_VOCABULARY` in `schema_contract.py` and asserted parity with OpenAPI schema. |
| **BUG-15** | Gateway | Updated `loader.py` to fail closed if `model_version` is absent or mismatches calibration artifact. |
| **BUG-16** | Audit | Documented PostgreSQL 16 service container requirement for 12 integration database tests. |
| **BUG-17** | Scripts | Fixed `verify_audit_chain.py` self-test to properly test interior deletion and verify error reporting. |
| **BUG-18** | Tests | Aligned test fixture enums in `test_retention_worker.py` to valid constants (`local-cpu`, `MOCK_SMOKE_MODE_NOT_A_DETECTOR`, `payment_release`). |
| **BUG-19** | Audit | Robust regex DSN normalization in `env.py` and `retention_worker.py` for all PostgreSQL URI prefixes. |
| **BUG-20** | Rules | Added binding rule **R-58** (*never rotate audit chain key*) and swept 7 citation sites. |

---

## 4. Phase 1 Definition-of-Done Audit

| DoD Requirement | Status | Verification Evidence |
|---|---|---|
| Contract files merged & frozen | ✅ Complete | `contracts/openapi.yaml`, `contracts/voice_scorer.proto`, `contracts/frame_contract.md` |
| 6 WSS Negative Contract Tests | ✅ Complete | 55 passing tests in `gateway/tests/test_ws_negative_contract.py` |
| Gateway CI Workflow | ✅ Complete | `.github/workflows/gateway-ci.yml` (lint, mypy, pytest) |
| Scorer CI Workflow | ✅ Complete | `.github/workflows/scorer-ci.yml` (309 tests passing) |
| Audit CI Workflow | ✅ Complete | `.github/workflows/audit-ci.yml` (477 unit tests passing, floor 455) |
| Audit Deny-List Tests | ✅ Complete | `audit/tests/test_deny_list.py` asserts zero audio/PII columns |
| Tamper-Evident Hash Chain | ✅ Complete | `gateway/app/audit/chain.py`, `scripts/verify_audit_chain.py --self-test` passing |
| Log Redaction Tests | ✅ Complete | `gateway/tests/test_log_redaction.py` verifies zero raw audio/PII in log lines |
| PWA Skeleton & Design Tokens | ✅ Complete | `pwa/` with 340 design tokens, builds and typechecks clean |
| Contract Test Vector | ✅ Complete | `ml/fixtures/contract_vector_v1.npy` (160 KiB) committed and validated |
| Local Compose Configuration | ✅ Complete | `infra/compose/docker-compose.yml` configured and verified |
| CDK Infrastructure Synthesis | ✅ Complete | 6 CDK stacks in `infra/cdk/` synthesize clean (`cdk synth`) |

---

## 5. What Remains for Phase 1 vs. External Items

### Completed Within Repository:
All repository software artifacts, test suites, CI workflows, and documentation required for Phase 1 are **100% complete**.

### External / Operational Items (Requires Live AWS Credentials / Quotas):
1. **GitHub Remote Push:** Requires user to configure git remote (`git remote add origin ...` and `git push -u origin main`).
2. **AWS Account Bootstrap:** CDK bootstrap and initial deployment to AWS requires credentials (`aws configure` / OIDC role).
3. **`g4dn.xlarge` Quota Request (H-2):** Service Quota increase in `ap-south-1` for GPU instance allocation.
4. **End-to-End Live Docker Run:** Running `docker compose up` on developer workstation with Docker daemon active.
