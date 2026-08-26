# Frame & Window Contract — the authoritative byte layout

**Owner:** Pair A · **Two-key review required** (see [CONTRACT_CHANGE_POLICY.md](CONTRACT_CHANGE_POLICY.md))
**Contract ID:** `raw-waveform-v1`

> `PS104_Five_Day_Implementation_Plan.md` §3 makes this a Day-1 blocker: *"Any ambiguity in
> sample rate, endian order, sequence, or context code blocks the next day."* This file is the
> single source of truth. Constants are mirrored in exactly two implementation files, and a CI
> test asserts they are equal:
> - `gateway/app/constants.py`
> - `pwa/src/lib/constants.ts`
>
> **Never inline these numbers anywhere else** ([rules.md](../rules.md) R-23).

---

## 1. Constants

| Name | Value | Derivation |
|---|---|---|
| `SAMPLE_RATE_HZ` | `16000` | product WSS contract |
| `CHANNELS` | `1` | mono |
| `SAMPLE_FORMAT` | `int16` **little-endian** | decision D-1 |
| `FRAME_MS` | `20` | WebRTC VAD supports 10/20/30 ms; 20 ms chosen |
| `SAMPLES_PER_FRAME` | `320` | `16000 × 0.020` |
| `BYTES_PER_FRAME_PAYLOAD` | `640` | `320 × 2` |
| `SEQ_PREFIX_BYTES` | `8` | `uint64` **big-endian** — decision D-2 |
| `WS_FRAME_BYTES` | **`648`** | `8 + 640` — decision D-3 |
| `WINDOW_MS` | `2560` | rolling analysis window |
| `WINDOW_SAMPLES` | `40960` | `16000 × 2.560` |
| `WINDOW_BYTES` | `81920` | `40960 × 2` |
| `HOP_MS` | `640` | score cadence |
| `HOP_SAMPLES` | `10240` | `16000 × 0.640` |
| `FRAMES_PER_HOP` | `32` | `10240 / 320` |
| `HOPS_PER_WINDOW` | `4` | `2560 / 640` → 75 % overlap |
| `ONNX_INPUT_SHAPE` | `[1, 40960]` `float32` | playbook §7 |
| `MAX_TEXT_FRAME_BYTES` | `4096` | `session.open` size guard |
| `TICKET_TTL_SECONDS` | `60` | blueprint §6.2 |

---

## 2. WebSocket binary frame — exactly 648 bytes

```
 byte  0 ─────────────── 7 │ 8 ────────────────────────── 647
┌──────────────────────────┬────────────────────────────────┐
│ sequence  uint64  BE     │ pcm  320 × int16  LE  (640 B)  │
└──────────────────────────┴────────────────────────────────┘
```

**Why the header and payload disagree on byte order — deliberately:**

- The **sequence** is a protocol header field. Network byte order (big-endian) is the convention
  for protocol headers, and the blueprint states it explicitly.
- The **PCM payload** is bulk sample data. Every browser target is little-endian, `Int16Array` in
  the PWA is native-LE, and WAV/RIFF is LE. Big-endian samples would force a per-sample byteswap
  on the hot path for zero benefit.

Encode/decode:

```python
# Python (gateway) — struct format
SEQ_STRUCT = ">Q"                      # uint64 big-endian
PCM_DTYPE  = "<i2"                     # int16 little-endian
seq = struct.unpack_from(">Q", frame, 0)[0]
pcm = np.frombuffer(frame, dtype="<i2", offset=8, count=320)
```

```ts
// TypeScript (PWA)
const buf = new ArrayBuffer(648);
const dv  = new DataView(buf);
dv.setBigUint64(0, BigInt(seq), false);            // false = big-endian
const pcm = new Int16Array(buf, 8, 320);           // platform-native = LE on all targets
```

### 2.1 Validation rules — reject, never coerce ([rules.md](../rules.md) R-24)

| Condition | Result |
|---|---|
| `len(frame) != 648` | `PROTO_FRAME_SIZE`, close 1003 |
| `seq != expected_next` (duplicate, gap, or rewind) | `PROTO_SEQUENCE`, close 1003 |
| First message not a valid `session.open` | `PROTO_FIRST_MESSAGE`, close 1003 |
| Text frame > 4096 bytes | `PROTO_PAYLOAD_TOO_LARGE`, close 1009 |

`sequence` starts at `0` and increments by exactly `1`. On reconnect the counter **resets to 0** —
a resumed session is a new stream, not a spliced one. Padding a short frame or trimming a long one
destroys the property that makes CPU/GPU parity checkable.

---

## 3. Window assembly

Only **voiced** samples accumulate. Silence never enters the buffer and is never scored
(playbook §1: *"do not classify a 20 ms frame in isolation or infer from silence"*).

```
20 ms frame ──► VAD ──► voiced? ──no──► DISCARD (not buffered, not counted)
                          │ yes
                          ▼
              append to ring buffer (maxlen = 40960 samples)
                          │
        voiced samples since last hop >= 10240 ?
                          │ yes
                          ▼
              ring buffer full (== 40960) ? ──no──► wait, no score request
                          │ yes
                          ▼
              emit 81920-byte window ──► gRPC ScoreWindow
```

Consequences that follow from this and must not be "optimized" away:

- **First decision needs ≥ 2.56 s of *voiced* audio**, which is more than 2.56 s of wall-clock
  time. Reporting first-decision latency from wall-clock start is therefore expected to exceed
  2.56 s; that is not a bug.
- The ring buffer is `deque(maxlen=40960)` so overflow is structurally impossible, not
  prevented by a check that could be removed.
- The buffer is process memory only, is never written to disk, and is cleared in a `finally`
  block on close, error, or disconnect ([rules.md](../rules.md) R-14).

---

## 4. gRPC window payload

`ScoreWindowRequest.pcm_window` is **exactly 81,920 bytes** — the same int16-LE encoding as the
frame payload, concatenated in sample order (oldest sample first).

`contract_id` must be `"raw-waveform-v1"`. `sample_rate_hz` must be `16000`.

---

## 5. PCM16 → float32, outside the ONNX graph

Playbook §7 requires the conversion to be documented outside the graph. It is exactly this, in
`scorer/app/contract.py`, and nowhere else:

```python
def pcm16_to_float32(pcm_bytes: bytes) -> np.ndarray:
    """int16 LE -> float32 in [-1, 1). Shape (1, 40960). No resampling, no normalization,
    no channel downmix (input is already mono). Divisor is 32768.0, not 32767.0."""
    if len(pcm_bytes) != 81920:
        raise ValueError(f"expected 81920 bytes, got {len(pcm_bytes)}")
    samples = np.frombuffer(pcm_bytes, dtype="<i2")
    return (samples.astype(np.float32) / 32768.0).reshape(1, 40960)
```

The divisor `32768.0` (not `32767.0`) is part of the contract. A mismatch between training
preprocessing and serving preprocessing is a silent, calibration-invalidating bug — and it is
exactly what the fixed contract test vector in §6 exists to catch.

**Explicitly outside the graph:** resampling, clipping policy, channel downmix, and output class
orientation. None may be implicit (playbook §7).

---

## 6. Fixed contract test vector

`ml/fixtures/contract_vector_v1.npy` — a fixed `float32` array of shape `(1, 40960)`, created in
Phase 1 by Pair B, used at three points:

1. **Phase 1** — score it with the PyTorch reference; record the expected value.
2. **Phase 3 ONNX parity gate** — PyTorch vs ONNX must match within the predeclared tolerance.
   Failure **blocks deployment**, full stop ([phases.md](../phases.md) §4.1).
3. **Every Scorer startup** — re-scored and compared. `HealthResponse.contract_vector_parity_ok`
   reports the result, so a mismatched artifact pairing cannot reach a demo unnoticed.

Declared tolerance: `atol=1e-4` on `raw_score`, **and** identical `high_window` boolean at the
policy threshold. Ranking agreement over the fixture set matters more than absolute delta — a
calibrated decision that flips is a failure even inside `atol`.

---

## 7. Version history

| Version | Date | Change | Author |
|---|---|---|---|
| `raw-waveform-v1` | 2026-08-26 | Initial. Resolves D-1 (PCM LE), D-2 (seq BE), D-3 (648 bytes) | Phase 0 bootstrap |
