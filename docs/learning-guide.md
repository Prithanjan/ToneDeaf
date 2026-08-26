# Engineering & Architectural Learning Guide — Voice Integrity Control Plane

**Audience:** Engineers, Researchers, and AI Agents onboarding to the SIH26104 / PS104 codebase.  
**Companion Documents:** [architecture.md](../architecture.md) · [rules.md](../rules.md) · [technical-design.md](../technical-design.md) · [memory.md](../memory.md)

---

## 1. System Mental Model

The **Voice Integrity Control Plane** protects high-stakes voice interactions (e.g., payment release, beneficiary change) by scoring realtime audio streams for synthetic speech and deepfake spoofing, without ever storing raw audio.

```
+-----------------------------------------------------------------------------------------+
|                                    CLIENT (PWA / SDK)                                   |
+-----------------------------------------------------------------------------------------+
       |                                                               |
       | 1. POST /api/v1/sessions (consent, purpose)                   | 3. WSS /ws/v1/stream
       | 2. POST /api/v1/stream-ticket (ephemeral JWT)                 |    (648-byte PCM frames)
       v                                                               v
+-----------------------------------------------------------------------------------------+
|                                  GATEWAY SERVICE                                        |
|  - Subprotocol Auth (`sih-ticket.<JWT>`)        - 3s Ring Buffer / Voicing VAD          |
|  - Policy Engine (Finite State Machine)         - Metrics & Advisory Diagnostics Sidecar |
+-----------------------------------------------------------------------------------------+
       |                                                               |
       | Internal gRPC / Protobuf                                      | Chained Audit Writes
       v                                                               v
+------------------------------------+                     +------------------------------+
|          SCORER SERVICE            |                     |       POSTGRESQL 16          |
|  - Linear Spectrogram Extraction   |                     |  - `audit_event` Table       |
|  - AASIST Model (ONNX Runtime)     |                     |  - HMAC-SHA256 Hash Chain    |
|  - Platt Calibration Transform     |                     |  - Closed Enum DDL Schemas   |
+------------------------------------+                     +------------------------------+
```

---

## 2. The 7 Core Tenets & Non-Negotiable Invariants

1. **No Raw Audio Persistence (R-14):**
   Raw audio exists in memory only inside the Gateway's short-lived ring buffer (3 seconds max). Audio is never written to disk, databases, S3, or logs.
2. **Closed Action Vocabulary (R-07):**
   The policy engine produces only: `continue`, `verify`, `hold`, `escalate`, `terminate`. Verbs like `approve` or `deny` are forbidden by construction.
3. **Calibrated Scores Required (R-11):**
   Raw model output is never treated as a probability. A model score must pass through the Platt calibration transform (`policy/calibration.json`) before policy evaluation.
4. **Mock Mode is Loud (R-46):**
   When running without a trained model, the detector mode is explicitly labeled `MOCK_SMOKE_MODE_NOT_A_DETECTOR`. Mock scores are never presented as real detections.
5. **Contract Discipline (R-24, R-27):**
   Wrong-shaped inputs are rejected outright, never coerced or patched. Changing hash-chain field sets requires a version bump and documented re-anchor.
6. **OIDC-Only CI/CD (R-55):**
   GitHub Actions connects to AWS exclusively via short-lived OIDC role assumption. Zero permanent AWS access keys in Git or secrets.
7. **Never Rotate Audit Chain Keys (R-58):**
   The HMAC key used to link audit events must never be rotated once rows exist. Rotating the key invalidates every historical hash and creates false tampering alarms.

---

## 3. Key Design Patterns & Lessons Learned

### 3.1 Exclusion-Based CI Testing vs Marker Selection
- **The Problem:** When workflows select tests with explicit markers (e.g. `pytest -m contract`), newly written unit tests that lack markers are silently skipped in CI.
- **The Solution:** Use exclusion filtering (`pytest -m "not integration"`) with passed-count minimum floors. Any new test is executed by default on commit.

### 3.2 DSN Normalization Across Python Drivers
- **The Problem:** SQLAlchemy requires `postgresql+asyncpg://` or `postgresql+psycopg2://`, whereas raw `asyncpg.connect()` rejects any `+driver` suffix and requires plain `postgresql://` or `postgres://`.
- **The Solution:** Implement comprehensive regex normalization:
  - Alembic `env.py`: rewrites all forms to `postgresql+asyncpg://`.
  - Application / Asyncpg: strips dialect suffixes down to `postgresql://`.

### 3.3 Fail-Closed Policy & Model Loading
- **The Problem:** Failing open on missing keys (e.g. `declared_model = raw.get("model_version")` without asserting presence) lets unversioned policies match any model distribution.
- **The Solution:** Validate required keys explicitly (`if not declared_model: raise PolicyLoadError(...)`) and fail at boot time before traffic arrives.

### 3.4 Whole-Session-Atomic Audit Retention
- **The Problem:** Naive retention (`DELETE FROM audit_event WHERE retention_expires_at <= now()`) deletes the oldest events of active sessions, breaking the hash chain for surviving rows and triggering false tampering alarms.
- **The Solution:** Delete only when *all* rows of a session have expired using `HAVING MAX(retention_expires_at) <= cutoff`.

---

## 4. Verification Protocol & Quality Checklist

Before submitting a pull request or promoting changes, run:

```bash
# 1. Run Gateway test suite
python -m pytest gateway/tests -v

# 2. Run Scorer test suite
python -m pytest scorer/tests -v

# 3. Run Audit unit test suite
python -m pytest audit/tests -m "not integration" -v

# 4. Run Audit Chain verifier self-test
python scripts/verify_audit_chain.py --self-test

# 5. Run CDK synthesis
cd infra/cdk && npm run synth
```

All suites must exit code 0 with 100% passing tests.
