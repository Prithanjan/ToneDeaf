"""The byte contract: window validation and the ONE documented PCM16 → float32 conversion.

Normative source: ``contracts/frame_contract.md`` §5 and §6, and ``technical-design.md`` §7.

WHY THE CONSTANTS ARE RE-DECLARED HERE INSTEAD OF IMPORTED
``gateway/app/constants.py`` is the single Python definition of the wire contract (rules.md R-23),
and importing it would be strictly better. It is not possible: the Scorer image contains
``scorer/app`` and nothing from ``gateway/`` (see ``scorer/Dockerfile``), the two services are
separate deployables with separate ECR repositories, and a build-time copy of the Gateway's module
into the Scorer image would create a second file with the same authority — exactly the drift
``contracts/CONTRACT_CHANGE_POLICY.md`` forbids.

So the values are pinned in ONE place per service, and the cross-service equality is asserted by a
test rather than by an import: ``scorer/tests/test_contract.py::TestGatewayConstantsParity`` parses
``gateway/app/constants.py`` as text and fails on any divergence. That is the same technique
``gateway/tests/test_constants_parity.py`` already uses against ``pwa/src/lib/constants.ts`` for the
same reason (the browser cannot import Python either), so this repo now has one pattern for
cross-boundary constant parity instead of two.

The Scorer deliberately does NOT define ``WS_FRAME_BYTES`` (648) or ``BYTES_PER_FRAME_PAYLOAD``
(640). It never sees a WebSocket frame — the Gateway assembles the window and the Scorer receives
only the assembled 81,920 bytes. Declaring them here would create a third copy of two numbers this
process cannot exercise, and an unexercised copy is one that drifts without any test noticing.
"""

from __future__ import annotations

from typing import Final

import numpy as np

# --- Audio format (decision D-1: PCM is int16 LITTLE-endian) -------------------------------------
CONTRACT_ID: Final[str] = "raw-waveform-v1"
SAMPLE_RATE_HZ: Final[int] = 16_000
CHANNELS: Final[int] = 1
PCM_DTYPE: Final[str] = "<i2"  # numpy: int16 little-endian
BYTES_PER_SAMPLE: Final[int] = 2

# --- Analysis window ------------------------------------------------------------------------------
WINDOW_MS: Final[int] = 2_560
WINDOW_SAMPLES: Final[int] = 40_960  # 16000 * 2.560
WINDOW_BYTES: Final[int] = 81_920  # 40960 * 2

# --- Model input ----------------------------------------------------------------------------------
ONNX_INPUT_BATCH: Final[int] = 1
ONNX_INPUT_SAMPLES: Final[int] = WINDOW_SAMPLES
ONNX_INPUT_SHAPE: Final[tuple[int, int]] = (ONNX_INPUT_BATCH, ONNX_INPUT_SAMPLES)

#: 32768.0, NOT 32767.0. Part of the contract, and the divisor the training preprocessing uses.
#:
#: Both divisors produce a float32 array of the correct shape and dtype, both keep every value inside
#: [-1, 1], and the relative difference is 3e-5 — which is below the eyeball threshold of anyone
#: comparing two waveform plots and below the atol of a naive parity check. So no shape test, no
#: dtype test, and no smoke test catches it. What it does change is the input distribution the model
#: sees relative to the one it was trained on, which shifts the raw score, which shifts the
#: post-Platt probability, which shifts where the k-of-n threshold actually sits. The failure mode is
#: a detector that still "works" and is quietly mis-calibrated.
#:
#: 32768.0 is also the arithmetically correct choice: int16 spans [-32768, +32767], so dividing by
#: 32768.0 maps that range onto [-1, +1) exactly, with no asymmetry between the rails.
PCM16_FLOAT_DIVISOR: Final[float] = 32_768.0

#: The int16 rails, derived from the divisor rather than typed twice. They are the clamp bounds for the
#: float32 → PCM inverse and the two sample values at which the 32767.0/32768.0 mistake is detectable
#: at all, so they are worth a name. Asymmetric by one: that asymmetry IS the contract.
INT16_MIN: Final[int] = -int(PCM16_FLOAT_DIVISOR)
INT16_MAX: Final[int] = int(PCM16_FLOAT_DIVISOR) - 1

# --- Quality thresholds ---------------------------------------------------------------------------
#: These describe the WINDOW, never the speaker (rules.md R-41, proto QualityFlag comment). Nothing
#: here may key on accent, emotion, illness, gender, age, or speaking style — and nothing here is
#: spoof evidence: a quality outcome only decides whether a window is ELIGIBLE to be counted
#: (rules.md R-09), never which way it counts.
LOW_ENERGY_RMS_FLOOR: Final[float] = 1.0e-4
CLIPPING_SAMPLE_FRACTION: Final[float] = 1.0e-3
CLIPPING_MAGNITUDE: Final[float] = 0.999
DC_OFFSET_MEAN_ABS: Final[float] = 1.0e-2


class ContractViolation(ValueError):
    """A request that does not match ``raw-waveform-v1``. Rejected, never coerced (rules.md R-24).

    ``detail`` is STATIC text chosen at construction from a fixed set. It is what the gRPC status
    carries back to the Gateway, and it interpolates nothing (rules.md R-17) — a message built from
    the request would let a caller reference or a payload fragment escape into the Gateway's log and
    from there into CloudWatch.

    ``expected`` and ``actual`` are carried as separate integers so the *log line* can still say what
    was wrong. They map onto the ``bytes_expected`` / ``bytes_actual`` keys already allow-listed in
    ``gateway/app/telemetry/logging.py``: a size is metadata about a payload, not the payload.
    """

    __slots__ = ("actual", "code", "detail", "expected")

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        expected: int | None = None,
        actual: int | None = None,
    ):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.expected = expected
        self.actual = actual


#: Static rejection messages. Listed together so it is visible at a glance that none of them contains
#: a format placeholder.
#:
#: Interpolated from the constants above at IMPORT time, not from the request at call time. That
#: distinction is the whole of rules.md R-17: these strings travel to the Gateway as a gRPC status
#: detail and from there into CloudWatch, so nothing caller-supplied may reach them — but the numbers
#: they quote are ours, and writing "81920" here by hand would put a fourth copy of WINDOW_BYTES in
#: the repo (rules.md R-23), one that a window-size change would leave behind saying the old number.
#: A rejection message that names the wrong expected size is worse than no message: it sends whoever
#: is debugging the framing bug looking for the wrong thing.
WRONG_WINDOW_SIZE: Final[str] = (
    f"pcm_window must be exactly {WINDOW_BYTES} bytes; not padded, trimmed, or resampled"
)
WRONG_CONTRACT_ID: Final[str] = f"contract_id must be {CONTRACT_ID}"
WRONG_SAMPLE_RATE: Final[str] = f"sample_rate_hz must be {SAMPLE_RATE_HZ}"

#: Not a request-rejection message: ``float32_to_pcm16`` is a fixture-builder path, never on the RPC
#: path. Kept here with the others so the "no placeholders" property holds for every detail string in
#: the module rather than for the three a reviewer happens to look at.
WRONG_FIXTURE_SHAPE: Final[str] = f"window must have shape {ONNX_INPUT_SHAPE}"


def validate_window_request(*, pcm_window: bytes, contract_id: str, sample_rate_hz: int) -> None:
    """Reject anything that is not the contract. Never coerce it into shape (rules.md R-24).

    An 81,919-byte window is not a window that needs one byte of padding; it is a bug somewhere in
    frame assembly, and padding it would hide that bug behind a score. The property this protects is
    the one that makes CPU/GPU parity checkable at all: both tiers must be scoring the same bytes.

    ``sample_rate_hz`` is checked to catch a wiring mistake, and for no other reason. A sampling rate
    is a channel characteristic, never spoof evidence (rules.md R-39) — nothing downstream of this
    function may branch on it.
    """
    if len(pcm_window) != WINDOW_BYTES:
        raise ContractViolation(
            "PROTO_WINDOW_SIZE",
            WRONG_WINDOW_SIZE,
            expected=WINDOW_BYTES,
            actual=len(pcm_window),
        )
    if contract_id != CONTRACT_ID:
        raise ContractViolation("PROTO_CONTRACT_ID", WRONG_CONTRACT_ID)
    if sample_rate_hz != SAMPLE_RATE_HZ:
        raise ContractViolation("PROTO_SAMPLE_RATE", WRONG_SAMPLE_RATE)


def pcm16_to_float32(pcm_bytes: bytes) -> np.ndarray:
    """int16 LE → float32 in [-1, 1), shape ``(1, 40960)``.

    THE conversion. It lives here, outside the ONNX graph, because ``playbook §7`` requires the
    preprocessing to be documented rather than implicit: the exported graph takes float32 that is
    already scaled, so anyone reading ``aasist.onnx`` cannot tell what the divisor was. Putting it in
    the graph would bury the single most calibration-sensitive constant in this system inside a binary
    artifact where no test could read it and no reviewer could see it.

    No resampling. No normalization. No channel downmix — the input is already mono, and a downmix
    step would silently accept stereo. Clipping policy is likewise absent: samples at the int16 rails
    stay at the rails and are *reported* as ``CLIPPING_DETECTED``, not repaired.
    """
    if len(pcm_bytes) != WINDOW_BYTES:
        # Defence in depth. validate_window_request already checked this at the RPC boundary; this
        # branch catches a caller that reaches the conversion by some other path (a fixture builder,
        # a future batch entry point) with a buffer that np.frombuffer would happily reinterpret at
        # the wrong length.
        raise ContractViolation(
            "PROTO_WINDOW_SIZE", WRONG_WINDOW_SIZE, expected=WINDOW_BYTES, actual=len(pcm_bytes)
        )

    samples = np.frombuffer(pcm_bytes, dtype=PCM_DTYPE)
    scaled = samples.astype(np.float32) / np.float32(PCM16_FLOAT_DIVISOR)
    return scaled.reshape(ONNX_INPUT_SHAPE)


def float32_to_pcm16(window: np.ndarray) -> bytes:
    """Inverse of :func:`pcm16_to_float32`, for fixtures and tests only.

    Exact for any array this module produced, because 32768.0 is a power of two: the forward divide
    and this multiply are both lossless in float32 over the int16 range. That exactness is the point —
    it lets the contract test vector be stored as float32 (as ``frame_contract.md`` §6 requires) while
    still being reachable as the exact PCM bytes a real ``ScoreWindow`` request would carry.

    Not used on the serving path. Nothing in this process ever needs to turn a score back into audio.
    """
    if window.shape != ONNX_INPUT_SHAPE:
        raise ContractViolation("FIXTURE_SHAPE", WRONG_FIXTURE_SHAPE)
    samples = np.rint(window.astype(np.float64) * PCM16_FLOAT_DIVISOR)
    return np.clip(samples, INT16_MIN, INT16_MAX).astype(PCM_DTYPE).tobytes()


def _self_check() -> None:
    """Arithmetic identities. Wrong here means the model is fed a differently-shaped world.

    Raises explicitly instead of asserting, and the difference is not stylistic. ``python -O`` strips
    every ``assert`` from the bytecode, so an assert-based self-check is silently absent in exactly the
    environment where nobody would look for it — an optimized container image. This function is the ONLY
    guard on the window arithmetic in a Scorer-only checkout, where the cross-service parity test in
    ``tests/test_contract.py`` is not run; a guard that can be turned off by an interpreter flag,
    without a line of code changing, is the kind that everyone believes is running.

    ``ContractViolation`` rather than a bare ``ValueError`` so that a build with a mis-derived window
    fails at IMPORT with the same exception type the request path uses, naming the identity that broke.
    The message names only our own constants, so R-17 is not in play.
    """
    identities: tuple[tuple[str, bool], ...] = (
        (
            "WINDOW_SAMPLES == SAMPLE_RATE_HZ * WINDOW_MS / 1000",
            WINDOW_SAMPLES == SAMPLE_RATE_HZ * WINDOW_MS // 1000,
        ),
        (
            "WINDOW_BYTES == WINDOW_SAMPLES * BYTES_PER_SAMPLE",
            WINDOW_BYTES == WINDOW_SAMPLES * BYTES_PER_SAMPLE,
        ),
        ("ONNX_INPUT_SAMPLES == WINDOW_SAMPLES", ONNX_INPUT_SAMPLES == WINDOW_SAMPLES),
        ("ONNX_INPUT_SHAPE == (1, WINDOW_SAMPLES)", ONNX_INPUT_SHAPE == (1, WINDOW_SAMPLES)),
        ("PCM16_FLOAT_DIVISOR == 2**15", PCM16_FLOAT_DIVISOR == 2.0**15),
        ("INT16_MIN/INT16_MAX are the int16 rails", (INT16_MIN, INT16_MAX) == (-32_768, 32_767)),
        ("CHANNELS == 1", CHANNELS == 1),
        (
            "PCM_DTYPE itemsize == BYTES_PER_SAMPLE",
            np.dtype(PCM_DTYPE).itemsize == BYTES_PER_SAMPLE,
        ),
        ("PCM_DTYPE is little-endian", np.dtype(PCM_DTYPE).byteorder in ("<", "=")),
    )
    for identity, holds in identities:
        if not holds:
            raise ContractViolation("CONTRACT_SELF_CHECK", f"broken contract identity: {identity}")


_self_check()
