"""Frame, window, and protocol constants.

THE ONLY PLACE THESE NUMBERS ARE DEFINED IN PYTHON.

Mirrored in ``pwa/src/lib/constants.ts``. A CI test
(``gateway/tests/test_constants_parity.py``) parses both files and asserts equality, because a
frame-size divergence between client and server is the most likely silent integration failure in
this build.

Normative source: ``contracts/frame_contract.md`` (contract id ``raw-waveform-v1``).
Never inline 648, 640, 81920, or 40960 anywhere else (rules.md R-23).
"""

from __future__ import annotations

from typing import Final

# --- Audio format (decision D-1: PCM is int16 LITTLE-endian) ------------------------------------
CONTRACT_ID: Final[str] = "raw-waveform-v1"
SAMPLE_RATE_HZ: Final[int] = 16_000
CHANNELS: Final[int] = 1
PCM_DTYPE: Final[str] = "<i2"  # numpy: int16 little-endian

# --- Frame -------------------------------------------------------------------------------------
FRAME_MS: Final[int] = 20
SAMPLES_PER_FRAME: Final[int] = 320  # 16000 * 0.020
BYTES_PER_FRAME_PAYLOAD: Final[int] = 640  # 320 * 2

# Decision D-2: the sequence header is uint64 BIG-endian (network order). The header and the
# payload deliberately disagree on byte order; see contracts/frame_contract.md section 2.
SEQ_PREFIX_BYTES: Final[int] = 8
SEQ_STRUCT: Final[str] = ">Q"

# Decision D-3: every binary WebSocket frame is EXACTLY this many bytes. Never coerced (R-24).
WS_FRAME_BYTES: Final[int] = 648  # 8 + 640

# --- Analysis window ---------------------------------------------------------------------------
WINDOW_MS: Final[int] = 2_560
WINDOW_SAMPLES: Final[int] = 40_960  # 16000 * 2.560
WINDOW_BYTES: Final[int] = 81_920  # 40960 * 2

HOP_MS: Final[int] = 640
HOP_SAMPLES: Final[int] = 10_240  # 16000 * 0.640
FRAMES_PER_HOP: Final[int] = 32  # 10240 / 320
HOPS_PER_WINDOW: Final[int] = 4  # 2560 / 640 -> 75% overlap

# --- Model input -------------------------------------------------------------------------------
ONNX_INPUT_BATCH: Final[int] = 1
ONNX_INPUT_SAMPLES: Final[int] = WINDOW_SAMPLES
# The divisor is part of the contract: 32768.0, NOT 32767.0. A mismatch between training and
# serving preprocessing is a silent, calibration-invalidating bug.
PCM16_FLOAT_DIVISOR: Final[float] = 32_768.0

# --- Protocol guards ---------------------------------------------------------------------------
MAX_TEXT_FRAME_BYTES: Final[int] = 4_096
TICKET_TTL_SECONDS: Final[int] = 60
WS_SUBPROTOCOL: Final[str] = "sih-v1"
WS_TICKET_SUBPROTOCOL_PREFIX: Final[str] = "sih-ticket."

# --- Audit -------------------------------------------------------------------------------------
# Bumping this is a breaking change requiring a documented re-anchor (R-27, decision D-9).
CHAIN_FIELD_SET_VERSION: Final[str] = "v1"
GENESIS_PREV_HASH: Final[bytes] = b"\x00" * 32


def _self_check() -> None:
    """Arithmetic identities. Wrong here means every downstream buffer size is wrong."""
    assert SAMPLES_PER_FRAME == SAMPLE_RATE_HZ * FRAME_MS // 1000
    assert BYTES_PER_FRAME_PAYLOAD == SAMPLES_PER_FRAME * 2
    assert WS_FRAME_BYTES == SEQ_PREFIX_BYTES + BYTES_PER_FRAME_PAYLOAD
    assert WINDOW_SAMPLES == SAMPLE_RATE_HZ * WINDOW_MS // 1000
    assert WINDOW_BYTES == WINDOW_SAMPLES * 2
    assert HOP_SAMPLES == SAMPLE_RATE_HZ * HOP_MS // 1000
    assert FRAMES_PER_HOP == HOP_SAMPLES // SAMPLES_PER_FRAME
    assert HOPS_PER_WINDOW == WINDOW_MS // HOP_MS
    assert WINDOW_SAMPLES % HOP_SAMPLES == 0


_self_check()
