# Technical Design — Low-Level Specification

**Status:** Authoritative low-level design. This document resolves every ambiguity that the
five-day plan lists as a Day-1 blocker.
**Companions:** [prd.md](prd.md) · [architecture.md](architecture.md) · [phases.md](phases.md) · [rules.md](rules.md) · [design.md](design.md) · [memory.md](memory.md)

> **Renamed 2026-08-26.** This file was previously `design.md`. It was moved because `design.md` is
> the *visual* design system (colour, type, spacing, components) and this is the *engineering* spec —
> two different audiences and two different review cadences sharing one filename. Section numbers are
> unchanged, so every existing citation of the form "§4.1", "§5.3", "§9" still resolves; only the
> filename in the citation moved. See [memory.md](memory.md) decision D-13.

> `PS104_Five_Day_Implementation_Plan.md` §3 Gateway row: *"Any ambiguity in sample rate, endian
> order, sequence, or context code blocks the next day."* §1–§3 below close all four.

---

## 1. Resolved ambiguities (decisions, with rationale)

| # | Ambiguity in source docs | **Decision** | Rationale |
|---|---|---|---|
| D-1 | PCM sample byte order never stated | **Signed 16-bit little-endian** | WAV/RIFF is LE; every browser target is LE; `Int16Array` in the PWA is native-LE. Big-endian samples would require a per-sample byteswap on the hot path for no benefit |
| D-2 | Frame sequence byte order | **`uint64` big-endian** (network order) — as the blueprint states | Explicit in blueprint §6.1. Keeping the *header* BE and *payload* LE is intentional and documented, not an accident |
| D-3 | Total WSS binary frame size never stated | **Exactly 648 bytes** = 8 (seq) + 640 (PCM) | 640 B = 320 samples = 20 ms @ 16 kHz. Any other length is a protocol error, never coerced |
| D-4 | Where `purpose_code` is declared | **At `POST /api/v1/sessions`**, echoed in `session.open` and **verified to match** | Binds purpose server-side *before* any audio exists. A mismatch is a protocol error. Stronger consent posture than accepting purpose on the audio channel |
| D-5 | `context_value_band` vocabulary undefined | **Closed enum:** `low` \| `medium` \| `high` \| `unspecified` | A free-string band is unauditable and invites PII leakage (e.g. an actual amount) |
| D-6 | Ticket reuse semantics undefined | **Single-use, 60 s TTL, bound to `session_id` + `sub`** | Replay protection. Blueprint's threat table requires a "replayed stream" negative test |
| D-7 | `tenant_id` timing | **Present from Phase 1** as `NOT NULL DEFAULT 'demo-tenant'` | Blueprint targets tenant isolation. Adding it later means a table rewrite *and* re-chaining every hash |
| D-8 | Alembic timing ("recommended target addition") | **Adopted in Phase 1**, not Phase 2 | The schema lands in Phase 1. Whichever phase creates the first table must create the first migration, or that table is forever un-migrated |
| D-9 | Which fields enter the hash chain | **A canonical, explicitly-ordered field list** (§5.3) — not `SELECT *` | `SELECT *` makes the chain break on any future additive migration. An explicit list makes chain-relevant changes a deliberate, version-bumped act |
| D-10 | Python version on this workstation | Containers pin `python:3.12-slim`; local dev **must** use 3.12 | Workstation has 3.14.5. `webrtcvad-wheels` / `onnxruntime` wheels for 3.14 are not assured |
| D-11 | First high-risk threshold | `0.78` recorded as **`derivation: placeholder`**, and CI blocks a `policy_eligible` release while it says so | Playbook §5: *"not a universally valid 0.78"*. It must be re-derived from a cost-sensitive matrix in Phase 2/3 |
| D-12 | Diagnostics plane wiring | Sidecar interface exists; **return value is discarded** by the policy engine until the ablation gate passes | Makes "advisory only" a code property rather than a promise |

---

## 2. Wire contract — WebSocket `/ws/v1/stream`

### 2.1 Handshake

```
GET /ws/v1/stream HTTP/1.1
Host: <edge>
Upgrade: websocket
Origin: https://<allow-listed-origin>          # REQUIRED, allow-list checked
Sec-WebSocket-Protocol: sih-v1, sih-ticket.<url-safe-b64-ticket>
```

- Server selects `sih-v1` and validates the `sih-ticket.` entry.
- The bearer JWT is **never** placed in the URL, query string, or a cookie.
- Rejections happen at handshake time where possible (HTTP 401/403), otherwise via close frame.

### 2.2 First message — `session.open` (JSON text frame)

```json
{
  "type": "session.open",
  "call_ref": "9f2c…",              // opaque HMAC pseudonym, from POST /sessions
  "purpose_code": "payment_release",
  "context_value_band": "high",
  "client_capture": { "sample_rate_hz": 16000, "frame_ms": 20, "path": "scriptprocessor" }
}
```

Rejected if: `call_ref` unknown/expired, `purpose_code` ≠ the server-side session record (D-4),
`context_value_band` outside the enum (D-5), any additional property present, or JSON > 4 KiB.

### 2.3 Binary frames — exactly 648 bytes

```
 byte  0 ─────────────── 7 │ 8 ────────────────────────── 647
┌──────────────────────────┬────────────────────────────────┐
│ sequence: uint64 BE      │ pcm: 320 × int16 LE (640 bytes)│
└──────────────────────────┴────────────────────────────────┘
```

| Field | Type | Rule |
|---|---|---|
| `sequence` | `uint64` **big-endian** | Starts at `0`, strictly `+1` monotonic. Duplicate or out-of-order → protocol error, connection closed |
| `pcm` | 320 × `int16` **little-endian** | 20 ms @ 16 kHz mono. `len(frame) != 648` → protocol error |

Derived constants (single source of truth: `contracts/frame_contract.md`):

| Constant | Value |
|---|---|
| `SAMPLE_RATE_HZ` | `16000` |
| `FRAME_MS` | `20` |
| `SAMPLES_PER_FRAME` | `320` |
| `BYTES_PER_FRAME_PAYLOAD` | `640` |
| `WS_FRAME_BYTES` | `648` |
| `WINDOW_MS` | `2560` |
| `WINDOW_SAMPLES` | `40960` |
| `WINDOW_BYTES` | `81920` |
| `HOP_MS` | `640` |
| `HOP_SAMPLES` | `10240` |
| `FRAMES_PER_HOP` | `32` |
| `HOPS_PER_WINDOW` | `4` (75 % overlap) |

### 2.4 Server → client events

| Event | Payload (fields only — never audio) |
|---|---|
| `session.accepted` | `session_id`, `policy_version`, `model_version`, `calibration_version`, `deployment_profile`, `execution_provider` |
| `risk.event` | `window_seq`, `spoof_risk`, `risk_state`, `eligible`, `quality_flags[]`, `occurred_at` |
| `policy.action` | `action`, `risk_state`, `purpose_code`, `policy_version`, `reason_code`, `audit_event_id` |
| `error` | `code`, `message` (static text only — **never** echo client input) |
| `session.closed` | `reason_code`, `windows_scored`, `buffer_cleared: true` |

### 2.5 Error / close codes

| App code | WS close | Meaning |
|---|---|---|
| `AUTH_TICKET_MISSING` | 1008 | No `sih-ticket.` subprotocol offered |
| `AUTH_TICKET_INVALID` | 1008 | Bad signature, expired, already used, or wrong `session_id` |
| `AUTH_ORIGIN_DENIED` | 1008 | `Origin` not on the allow-list |
| `PROTO_FRAME_SIZE` | 1003 | Binary frame length ≠ 648 |
| `PROTO_SEQUENCE` | 1003 | Non-monotonic / duplicate sequence |
| `PROTO_FIRST_MESSAGE` | 1003 | First frame was not a valid `session.open` |
| `PROTO_PURPOSE_MISMATCH` | 1008 | `session.open.purpose_code` ≠ session record |
| `PROTO_PAYLOAD_TOO_LARGE` | 1009 | Text frame > 4 KiB |
| `SESSION_ALREADY_STREAMING` | 1008 | Session already has a live connection |
| `BACKPRESSURE_REJECT` | 1013 | Gateway refuses rather than queue unbounded audio |
| `SCORER_UNAVAILABLE` | 1011 | gRPC deadline / unavailable |

**Error messages are static.** Echoing client input into an error string is a documented way for
a caller reference to escape into a log.

---

## 3. gRPC contract — `VoiceScorer`

```proto
service VoiceScorer {
  rpc ScoreWindow (ScoreWindowRequest) returns (ScoreWindowResponse);
  rpc Health      (HealthRequest)      returns (HealthResponse);
}
```

| Field | Rule |
|---|---|
| `ScoreWindowRequest.pcm_window` | **exactly 81,920 bytes**, int16 LE, 16 kHz mono. Wrong size → `INVALID_ARGUMENT`, never coerced |
| `ScoreWindowRequest.contract_id` | must equal `"raw-waveform-v1"` |
| `ScoreWindowRequest.sample_rate_hz` | must equal `16000` |
| `ScoreWindowRequest.session_ref` | HMAC pseudonym only. **Never** the raw client reference |
| `ScoreWindowResponse.spoof_risk` | calibrated, `[0,1]` |
| `ScoreWindowResponse.raw_score` | pre-calibration; diagnostics/parity only. **Not** a policy input |
| `ScoreWindowResponse.eligible` | `false` when quality flags disqualify the window from the k-of-n count |
| `ScoreWindowResponse.detector_mode` | `REAL_DETECTOR` \| `MOCK_SMOKE_MODE_NOT_A_DETECTOR` |

The Scorer receives **no** `purpose_code`, **no** session history, and **no** ability to return an
action. The detection/decision seam is enforced by this message shape.

---

## 4. Gateway internal design

### 4.1 Module layout

```
gateway/app/
  main.py                 # app factory, lifespan, startup banner (prints the parity set)
  config.py               # pydantic-settings; fails fast on missing secrets
  constants.py            # the ONLY place frame/window constants are defined
  security/
    jwt.py                # ONE validation path; Cognito & local JWKS differ by config only
    ticket.py             # HMAC ticket sign/verify, single-use replay cache
    pseudonym.py          # HMAC-SHA256 call_ref
    origin.py             # Origin allow-list
  api/v1/
    sessions.py           # POST /api/v1/sessions
    stream_ticket.py      # POST /api/v1/stream-ticket
    health.py             # /healthz, /readyz, /api/v1/version
  ws/
    stream.py             # WSS endpoint, session lifecycle
    frames.py             # parse/validate 648-byte frames  (pure, exhaustively tested)
  audio/
    vad.py                # webrtcvad wrapper, 20 ms frames
    ring.py               # 2.56 s voiced ring buffer + 640 ms hop trigger (pure, testable)
  policy/
    engine.py             # k-of-n state machine (pure function of a score sequence)
    loader.py             # policy.yaml + calibration.json load, hash, eligibility check
    diagnostics.py        # sidecar interface; return value DISCARDED until ablation gate
  scorer/client.py        # gRPC client, deadline, bounded concurrency
  audit/
    chain.py              # canonical serialization + HMAC chain (pure)
    writer.py             # asyncpg insert
  telemetry/
    logging.py            # redacting structured logger
    metrics.py            # versioned metric schema
```

**Design rule:** `frames.py`, `ring.py`, `engine.py`, and `chain.py` are **pure and
side-effect-free**. They are the four places a privacy or correctness bug would be most expensive,
so they are the four places that are exhaustively unit-testable without I/O.

### 4.2 The ring buffer (`audio/ring.py`)

```
frame (320 samples) ──► VAD ──► voiced? ──no──► DISCARD (not buffered, not counted)
                                    │yes
                                    ▼
                        append to deque(maxlen=40960)
                                    │
                     voiced_since_last_hop >= 10240 ?
                                    │yes
                                    ▼
                   buffer full (40960)? ──no──► wait (no score request)
                                    │yes
                                    ▼
                     emit 81920-byte window → ScoreWindow
```

Invariants (each has a test):
1. Only voiced samples are ever appended. Silence never enters the buffer.
2. `len(buffer) <= 40960` always — a `deque(maxlen=…)` makes overflow structurally impossible.
3. No score is requested before the buffer is full.
4. `clear()` zeroes the backing storage and is called on close, error, **and** in a `finally`.
5. Nothing in this module opens a file, socket, or DB handle.

### 4.3 Policy engine (`policy/engine.py`)

A pure state machine. Input: an ordered sequence of `(spoof_risk, eligible)`. Output:
`(risk_state, action, reason_code)`.

```
                    ┌──────────────┐
   window scored ──►│  collecting  │  fewer than n eligible windows seen
                    └──────┬───────┘
                           │  n eligible windows available
                           ▼
              count(high) over last n eligible
                 │                      │
        < k high │                      │ >= k high
                 ▼                      ▼
          ┌──────────────┐        ┌──────────┐
          │  uncertain   │        │   high   │   (sticky for the session)
          └──────────────┘        └──────────┘
```

- `high_window` ⇔ `spoof_risk >= thresholds.high_window_risk` **and** `eligible == true`.
- Ineligible windows are **skipped**, not counted as low. A codec-degraded window is not evidence
  of innocence.
- `high` is **sticky** for the session: evidence of manipulation does not evaporate because the
  next window looked clean. Un-sticking requires a human resolution step (Phase 4).
- Action = `purpose_actions[purpose_code][risk_state]`.
- The `Action` enum contains exactly `continue`, `verify`, `hold`, `escalate`. `approve` / `deny`
  are absent by construction, so "add an approve path" cannot be a one-line change.

Reason codes are emitted for the Privacy Inspector: `EVIDENCE_BELOW_K`, `EVIDENCE_K_OF_N_MET`,
`INSUFFICIENT_ELIGIBLE_WINDOWS`, `QUALITY_DEGRADED`.

---

## 5. Audit schema

### 5.1 Allowed columns — the complete list

| Column | Type | Notes |
|---|---|---|
| `event_id` | `uuid` PK | |
| `tenant_id` | `text NOT NULL DEFAULT 'demo-tenant'` | D-7 forward-compat for RLS |
| `session_id` | `uuid NOT NULL` | |
| `call_ref` | `text NOT NULL` | HMAC-SHA256 hex. **Never** the raw reference |
| `event_seq` | `bigint NOT NULL` | per-session, `UNIQUE (session_id, event_seq)` |
| `occurred_at` | `timestamptz NOT NULL` | |
| `purpose_code` | `text NOT NULL` | |
| `context_value_band` | `text NOT NULL` | enum-checked |
| `window_seq` | `bigint NULL` | null for lifecycle events |
| `spoof_risk` | `numeric(5,4) NULL` | calibrated only |
| `risk_state` | `text NOT NULL` | `collecting` \| `uncertain` \| `high` |
| `action` | `text NOT NULL` | `continue` \| `verify` \| `hold` \| `escalate` (CHECK constraint) |
| `reason_code` | `text NOT NULL` | |
| `policy_version` | `text NOT NULL` | |
| `policy_bundle_sha256` | `text NOT NULL` | |
| `model_version` | `text NOT NULL` | |
| `model_sha256` | `text NOT NULL` | |
| `calibration_version` | `text NOT NULL` | |
| `calibration_sha256` | `text NOT NULL` | |
| `quality_flags` | `text[] NOT NULL DEFAULT '{}'` | |
| `detector_mode` | `text NOT NULL` | mock mode is visible in the audit trail |
| `execution_provider` | `text NOT NULL` | `CUDAExecutionProvider` \| `CPUExecutionProvider` |
| `deployment_profile` | `text NOT NULL` | `aws-gpu` \| `local-cpu` |
| `prev_event_hash` | `bytea NOT NULL` | 32 bytes; genesis = 32 × `0x00` |
| `event_hash` | `bytea NOT NULL` | 32 bytes |
| `retention_expires_at` | `timestamptz NOT NULL` | retention worker target |

### 5.2 Structural deny-list (enforced, not promised)

The Phase-1 test asserts against `information_schema`, so a forbidden column cannot be added by a
later migration without the test failing:

1. **No** column name matching `%audio%`, `%pcm%`, `%waveform%`, `%transcript%`, `%embedding%`,
   `%phone%`, `%msisdn%`, `%caller_name%`, `%raw%`.
2. **No** `bytea` column other than `prev_event_hash` and `event_hash`.
3. **No** `vector` / `float[]`-shaped column anywhere in the schema.
4. **No** column wider than 512 bytes that is not on the allow-list above.
5. The allow-list itself is asserted as an **exact set** — an unexpected *extra* column fails the
   test too. Additive schema changes are therefore a deliberate act with a test update, not drift.

### 5.3 Hash chain

```
canonical = json({
  tenant_id, session_id, call_ref, event_seq, occurred_at(RFC3339 µs, UTC),
  purpose_code, context_value_band, window_seq, spoof_risk(4dp or null),
  risk_state, action, reason_code, policy_version, policy_bundle_sha256,
  model_version, model_sha256, calibration_version, calibration_sha256,
  quality_flags(sorted), detector_mode, execution_provider, deployment_profile
}, sort_keys=True, separators=(',',':'), ensure_ascii=False)

event_hash = HMAC_SHA256(audit_chain_key, canonical.encode('utf-8') || prev_event_hash)
```

- `event_id` and `retention_expires_at` are **excluded** — a retention change must not invalidate
  history.
- Field list is explicit (D-9) and versioned by `CHAIN_FIELD_SET_VERSION`. Changing it is a
  breaking change requiring a documented re-anchor.
- Verifier: recompute forward from genesis; on mismatch report the **first** divergent
  `event_seq`. `audit_hash_verification_failures` must be 0.

---

## 6. PWA design

```
pwa/src/
  App.tsx
  components/
    ConsentNotice.tsx     # BLOCKS getUserMedia until acknowledged
    SessionSetup.tsx      # purpose_code + context_value_band selection
    RiskTimeline.tsx      # per-window evidence, NEVER a bare score
    ActionBanner.tsx      # continue / verify / hold / escalate
    PrivacyInspector.tsx  # Phase 4
  lib/
    auth.ts               # Cognito SRP (MVP) | local test issuer
    api.ts                # POST /sessions, POST /stream-ticket
    capture.ts            # ScriptProcessor now; AudioWorklet is the target
    stream.ts             # WSS client, framing, reconnect/backoff
    constants.ts          # MIRRORS gateway constants — CI asserts equality
```

- **Capture ordering is a privacy control, not UX:** `getUserMedia` is unreachable in the
  component tree until `ConsentNotice` has been acknowledged.
- Capture path (`ScriptProcessor`) is labelled in the UI and in `session.open.client_capture` so
  the current-state honesty in [architecture.md](architecture.md) §3 is visible at runtime.
- Reconnect uses exponential backoff with jitter and **resets the sequence counter** on a new
  connection — a resumed session is a new stream, not a spliced one.
- `lib/constants.ts` and `gateway/app/constants.py` are compared by a CI test. A frame-size
  divergence between client and server is the most likely silent integration failure in this build.

---

## 7. Scorer design

```
scorer/app/
  server.py       # gRPC server, bounded worker pool
  model.py        # ONNX session, provider assertion, warmup
  calibration.py  # Platt transform, artifact hash check
  contract.py     # 81,920-byte input assertion + PCM16→float conversion
  banner.py       # startup banner: provider, model SHA, calibration SHA, detector_mode
```

Startup sequence, in order, **failing fast**:
1. Load `policy/calibration.json`, verify `model_sha256` matches the loaded ONNX file's SHA-256.
2. Create the ORT session; assert the *requested* provider is actually in
   `get_providers()`. A silent CPU fallback on the GPU tier is a **failure**, not a degradation —
   it would invalidate every latency number recorded that day.
3. Run the fixed 40,960-sample contract test vector; compare against the stored expected score
   within the declared tolerance.
4. Print the banner. Serve.

`PCM16 → float32` conversion is **outside** the ONNX graph, in `contract.py`, as one documented
function: `int16 → float32 / 32768.0`, no resampling, no normalization, no channel downmix
(input is already mono).

Mock mode: `DETECTOR_MODE=MOCK_SMOKE_MODE_NOT_A_DETECTOR` returns a deterministic score sequence
from `session_ref`, stamps `detector_mode` into every response and audit row, and **refuses to
start** if the release manifest asserts `policy_eligible`.

---

## 8. Configuration surface

One config object; the tier is a value, never a code branch ([rules.md](rules.md) R-04).

| Variable | `local-cpu` | `aws-gpu` |
|---|---|---|
| `DEPLOYMENT_PROFILE` | `local-cpu` | `aws-gpu` |
| `EXECUTION_PROVIDER` | `CPUExecutionProvider` | `CUDAExecutionProvider` |
| `JWT_ISSUER` | `https://testidp:8081` | Cognito issuer URL |
| `JWT_JWKS_URL` | `http://testidp:8081/.well-known/jwks.json` | Cognito JWKS URL |
| `ALLOWED_ORIGINS` | `https://sih26104.local` | `https://<cf-domain>` |
| `SCORER_TARGET` | `scorer:50051` | `scorer.sih26104.local:50051` |
| `DATABASE_URL` | Compose DNS | RDS endpoint |
| `HMAC_KEY` / `TICKET_SIGNING_KEY` / `AUDIT_CHAIN_KEY` | Docker secret / `.env` (git-ignored) | Secrets Manager |
| `ORT_INTRA_OP_THREADS` | from the measured sweep | unset (GPU) |
| `POLICY_BUNDLE_PATH` | `/policy/policy.yaml` | same |

Fail-fast rules: missing secret → refuse to start. `JWT_ISSUER` pointing at the test issuer while
`DEPLOYMENT_PROFILE=aws-gpu` → **refuse to start**. A demo-only issuer reachable from a
production-shaped deployment is exactly the confusion `research-evidence.md` demands be prevented.

---

## 9. Test strategy

| Layer | What | Phase |
|---|---|---|
| Pure unit | `frames`, `ring`, `engine`, `chain`, `pseudonym`, `ticket` | 1 |
| WSS negative contract | missing ticket, wrong Origin, duplicate sequence, wrong byte length, purpose mismatch, oversized text | **1 (exit criteria)** |
| Schema deny-list | structural assertion against `information_schema` | **1 (exit criteria)** |
| Chain tamper | alter one row in a copy → verifier fails at that exact `event_seq` | **1 (exit criteria)** |
| Contract compatibility | protobuf back-compat + OpenAPI hash + client/server constant equality | 1 |
| Log redaction | inject a caller ref + PCM into every logger call site; assert absent from output | 1 |
| Policy sequence | deterministic score sequences → expected state/action traces | 2 |
| Integration (Compose) | full stack → chained, audio-free audit row | 2 |
| ONNX parity | fixed vector, PyTorch vs ONNX within tolerance | **3 (deploy blocker)** |
| Retention | controlled clock → expired rows deleted | 3 |
| Dual-tier parity | same trace, same hashes, both profiles | 5 |

The whole suite is parameterized by `BASE_URL` so the **same tests** run against AWS and local
(five-day plan §2).
