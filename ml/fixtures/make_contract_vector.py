"""Generate ``ml/fixtures/contract_vector_v1.npy`` — the fixed window used at every parity gate.

    python ml/fixtures/make_contract_vector.py            # write the fixture
    python ml/fixtures/make_contract_vector.py --check     # verify the committed file matches

Normative source: ``contracts/frame_contract.md`` §6. The same 40,960-sample float32 vector is scored:

* in Phase 1 by the PyTorch checkpoint, to record the expected raw score;
* in Phase 3 by the exported ONNX graph, as the parity gate that must match within ``atol=1e-4``;
* at **every Scorer startup**, whose result becomes ``HealthResponse.contract_vector_parity_ok``.

WHY THE CONTENT IS DETERMINISTIC INTEGER NOISE

*Bit-exact and platform-independent.* The samples come from an explicit 64-bit LCG evaluated in Python
integer arithmetic — no ``np.sin``, no RNG object, no float accumulation. ``np.sin`` is libm-dependent in
the last bit and a seeded ``Generator`` is a NumPy-version promise; either would let the committed
fixture differ from a regenerated one on a different machine, and a parity fixture that is only nearly
reproducible cannot be the thing other parity checks are measured against.

*Broadband, on purpose.* A single tone would survive an accidental resample, a stray low-pass, or a
1/32767-vs-1/32768 scaling error with a barely-changed score, because almost all of its energy sits in
one bin. Noise excites every bin, so any of those bugs moves the raw score by far more than
``atol=1e-4``. The fixture's job is to be maximally sensitive to preprocessing drift, not to sound like
anything.

*Synthetic, so there is no data question.* A committed speech clip would be a voice recording in Git
with a provenance and consent story attached (rules.md R-14, R-42). Integer noise has neither.

THE FIXTURE PINS THE 32768.0 DIVISOR

It is produced by calling ``scorer.app.contract.pcm16_to_float32`` on the generated PCM rather than by
scaling inline here. So the committed ``.npy`` is a frozen record of what that function did on the day
it was generated: change the divisor to 32767.0 and ``--check`` fails on a byte comparison, and
``scorer/tests/test_contract.py::TestPcm16Divisor`` fails on the value. Two independent tests, one of
which does not need to know what the right number is — only that it has not changed.

The vector deliberately includes 4 samples at ``-32768`` and 4 at ``+32767``. Those are the two int16
rails, and they are the only samples at which 32767.0 and 32768.0 differ by more than a rounding
wobble: ``-32768/32767`` is ``-1.0000305``, outside the closed interval the contract declares. Eight
rail samples is 1.95e-4 of the window — below ``CLIPPING_SAMPLE_FRACTION`` (1e-3), so the fixture is
still an ELIGIBLE window and can be reused as a valid request payload in server tests.

This file is a generator, not a runtime dependency. Nothing in ``scorer/app`` imports it.
"""

from __future__ import annotations

import argparse
import sys
from hashlib import sha256
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The conversion under test lives in the Scorer, which is a separate deployable with no package
# metadata (see scorer/pyproject.toml — no [project] table, deliberately). Importing it by path is what
# lets this fixture be produced BY the contract function instead of by a second copy of it here; a
# second copy would make the fixture agree with itself and with nothing else.
sys.path.insert(0, str(_REPO_ROOT / "scorer"))

from app.contract import (  # noqa: E402 - must follow the sys.path insert above
    ONNX_INPUT_SHAPE,
    PCM_DTYPE,
    WINDOW_BYTES,
    WINDOW_SAMPLES,
    pcm16_to_float32,
)

FIXTURE_PATH = _REPO_ROOT / "ml" / "fixtures" / "contract_vector_v1.npy"

#: MMIX (Knuth) 64-bit LCG. Chosen because the constants are published and the recurrence is one line,
#: so anyone can reimplement this in another language and get the same bytes — which matters if the
#: fixture ever has to be regenerated outside Python.
_LCG_MULTIPLIER = 6364136223846793005
_LCG_INCREMENT = 1442695040888963407
_LCG_MASK = (1 << 64) - 1

#: Any fixed value works; this one is the problem-statement number so its provenance is obvious.
_SEED = 26104

#: ~0.24 of full scale. High enough that RMS is far above LOW_ENERGY_RMS_FLOOR (so the fixture is an
#: eligible window), low enough that the deliberate rail samples remain the only extreme values.
_AMPLITUDE = 8_000

#: Fixed positions for the int16 rails, spread across the window so a bug that only touches one end of
#: the buffer still hits one.
_RAIL_POSITIONS: tuple[tuple[int, int], ...] = (
    (0, -32_768),
    (1, 32_767),
    (WINDOW_SAMPLES // 4, -32_768),
    (WINDOW_SAMPLES // 4 + 1, 32_767),
    (WINDOW_SAMPLES // 2, -32_768),
    (WINDOW_SAMPLES // 2 + 1, 32_767),
    (WINDOW_SAMPLES - 2, -32_768),
    (WINDOW_SAMPLES - 1, 32_767),
)


def build_pcm_bytes() -> bytes:
    """The exact 81,920 bytes a real ``ScoreWindow`` request would carry for this fixture.

    Pure integer arithmetic end to end. The ``& _LCG_MASK`` is the modulus and the ``>> 33`` discards
    the low bits, which are the weakest in any LCG — sampling from the top half is what stops the
    low-order bits from carrying a visible period.
    """
    state = _SEED & _LCG_MASK
    samples: list[int] = []
    for _ in range(WINDOW_SAMPLES):
        state = (_LCG_MULTIPLIER * state + _LCG_INCREMENT) & _LCG_MASK
        # Top 31 bits → a symmetric integer in [-_AMPLITUDE, +_AMPLITUDE]. Symmetric so the window's
        # mean stays near zero and the fixture does not trip DC_OFFSET.
        draw = (state >> 33) & 0x7FFF_FFFF
        samples.append(((draw * (2 * _AMPLITUDE + 1)) >> 31) - _AMPLITUDE)

    for index, value in _RAIL_POSITIONS:
        samples[index] = value

    pcm = np.asarray(samples, dtype=PCM_DTYPE).tobytes()
    if len(pcm) != WINDOW_BYTES:
        raise AssertionError("generated PCM is not exactly 81920 bytes")
    return pcm


def build_contract_vector() -> np.ndarray:
    """``(1, 40960)`` float32, produced by the contract's own conversion function."""
    vector = pcm16_to_float32(build_pcm_bytes())
    if vector.shape != ONNX_INPUT_SHAPE or vector.dtype != np.float32:
        raise AssertionError("generated vector does not match the model input contract")
    return vector


def _describe(vector: np.ndarray, payload: bytes) -> str:
    samples = vector.reshape(-1).astype(np.float64)
    return (
        f"shape={vector.shape} dtype={vector.dtype} "
        f"min={samples.min():+.8f} max={samples.max():+.8f} "
        f"mean={samples.mean():+.3e} rms={np.sqrt(np.mean(samples**2)):.6f}\n"
        f"sha256={sha256(payload).hexdigest()}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate or verify contract_vector_v1.npy"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed fixture is byte-identical to a fresh generation",
    )
    args = parser.parse_args(argv)

    vector = build_contract_vector()

    # allow_pickle is left at its default for saving a plain float32 array (no object dtype is
    # possible here), but every LOAD of this file — in server.py and in the tests — passes
    # allow_pickle=False, because a .npy can carry a pickle and unpickling is code execution.
    if args.check:
        if not FIXTURE_PATH.is_file():
            print(f"MISSING: {FIXTURE_PATH}", file=sys.stderr)
            return 1
        committed = np.load(FIXTURE_PATH, allow_pickle=False)
        if committed.shape != vector.shape or committed.dtype != vector.dtype:
            print(
                "MISMATCH: committed fixture has a different shape or dtype",
                file=sys.stderr,
            )
            return 1
        # Bit-exact, not allclose. This comparison is the one that catches a changed divisor, and a
        # tolerance would let 32767.0 through: the relative difference is 3e-5.
        if not np.array_equal(committed, vector):
            print(
                "MISMATCH: committed fixture differs from a fresh generation. Either this generator "
                "changed or scorer/app/contract.py::pcm16_to_float32 changed. Regenerating is NOT the "
                "fix until you know which — the ONNX parity gate and every recorded raw score are "
                "measured against this file (frame_contract.md §6).",
                file=sys.stderr,
            )
            return 1
        print(
            f"OK  {FIXTURE_PATH.name}\n{_describe(vector, FIXTURE_PATH.read_bytes())}"
        )
        return 0

    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    np.save(FIXTURE_PATH, vector)
    print(f"wrote {FIXTURE_PATH}\n{_describe(vector, FIXTURE_PATH.read_bytes())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
