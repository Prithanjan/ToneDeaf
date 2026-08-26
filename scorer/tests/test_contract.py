"""The byte contract: the divisor, the window size, and cross-service constant parity.

Every test here guards a failure that produces no exception and no visibly wrong output — which is why
they exist as tests rather than as runtime checks.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pytest

from app import contract
from app.contract import (
    CONTRACT_ID,
    ONNX_INPUT_SHAPE,
    PCM16_FLOAT_DIVISOR,
    SAMPLE_RATE_HZ,
    WINDOW_BYTES,
    WINDOW_SAMPLES,
    ContractViolation,
    float32_to_pcm16,
    pcm16_to_float32,
    validate_window_request,
)
from tests.conftest import CONTRACT_VECTOR_PATH, REPO_ROOT


def _int16_extremes_pcm() -> bytes:
    """A full window whose first two samples are the int16 rails, the rest zero."""
    samples = np.zeros(WINDOW_SAMPLES, dtype="<i2")
    samples[0] = -32_768
    samples[1] = 32_767
    return samples.tobytes()


class TestPcm16Divisor:
    """The divisor is 32768.0. Wrong here, nothing fails and the calibration is quietly invalid."""

    def test_divisor_is_two_to_the_fifteen(self) -> None:
        """Prevents a 32767.0 'fix' landing because someone reasoned from int16's maximum.

        int16 spans [-32768, +32767]. Dividing by 32768.0 maps that onto [-1, +1) exactly. Dividing by
        32767.0 maps it onto [-1.0000305, +1], which puts the negative rail OUTSIDE the interval the
        contract declares and shifts every sample by a factor of 1.0000305 relative to the distribution
        the model was trained on.
        """
        assert PCM16_FLOAT_DIVISOR == 2.0**15
        assert PCM16_FLOAT_DIVISOR == 32_768.0

    def test_negative_rail_maps_to_exactly_minus_one(self) -> None:
        """Prevents the off-by-one divisor. This is the only sample value that proves which one is used.

        With 32768.0 the negative rail is exactly -1.0. With 32767.0 it is -1.0000305. Both are float32,
        both are in shape, both plot identically — so no shape test, dtype test, or eyeball comparison
        distinguishes them. This assertion does.
        """
        window = pcm16_to_float32(_int16_extremes_pcm())
        assert window[0, 0] == -1.0
        assert window[0, 1] == np.float32(32_767) / np.float32(32_768.0)

    def test_the_wrong_divisor_would_produce_a_different_answer(self) -> None:
        """Proves the test above actually discriminates, rather than passing for both divisors.

        A test that would pass under the bug it claims to catch is worse than no test: it is a green
        check mark next to an unverified property. This computes the 32767.0 result explicitly and
        asserts the contract's output is not it.
        """
        pcm = _int16_extremes_pcm()
        correct = pcm16_to_float32(pcm)
        wrong = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / np.float32(32_767.0)
        assert correct[0, 0] != wrong[0]
        assert float(wrong[0]) < -1.0  # the reason 32767.0 is wrong, stated as an assertion

    def test_conversion_stays_inside_the_declared_interval(self) -> None:
        """Prevents a value outside [-1, 1) reaching the graph, which no downstream check would catch."""
        window = pcm16_to_float32(_int16_extremes_pcm())
        assert window.min() >= -1.0
        assert window.max() < 1.0

    def test_divisor_is_not_applied_inside_the_onnx_graph(self) -> None:
        """Prevents the divisor migrating into the exported graph, where no test could read it.

        Asserted structurally: the conversion is a plain Python function in this module, and it is what
        server.py calls before touching the detector. If preprocessing moved into the graph, this
        function would become dead code and the most calibration-sensitive constant in the system would
        live inside a binary artifact.
        """
        source = Path(contract.__file__).read_text(encoding="utf-8")
        assert "def pcm16_to_float32" in source
        server_source = (Path(contract.__file__).parent / "server.py").read_text(encoding="utf-8")
        assert "pcm16_to_float32(request.pcm_window)" in server_source


class TestWindowValidation:
    """Reject, never coerce (rules.md R-24)."""

    def test_exact_window_is_accepted(self, valid_pcm: bytes) -> None:
        validate_window_request(
            pcm_window=valid_pcm, contract_id=CONTRACT_ID, sample_rate_hz=SAMPLE_RATE_HZ
        )

    @pytest.mark.contract
    @pytest.mark.parametrize("delta", [-1, 1, -2, 2, -WINDOW_BYTES, WINDOW_BYTES])
    def test_off_by_one_window_is_rejected(self, valid_pcm: bytes, delta: int) -> None:
        """Prevents a mis-assembled window being padded or trimmed into shape and then scored.

        An 81,919-byte window is not a window missing one byte of padding; it is a bug in frame
        assembly. Padding it would produce a score, and the score would look fine — which is how a
        framing bug survives to Day 5. It also breaks the only property that makes CPU/GPU parity
        checkable: both tiers must be scoring the same bytes.
        """
        payload = (valid_pcm + b"\x00" * max(delta, 0))[: WINDOW_BYTES + delta]
        with pytest.raises(ContractViolation) as excinfo:
            validate_window_request(
                pcm_window=payload, contract_id=CONTRACT_ID, sample_rate_hz=SAMPLE_RATE_HZ
            )
        assert excinfo.value.code == "PROTO_WINDOW_SIZE"
        assert excinfo.value.expected == WINDOW_BYTES
        assert excinfo.value.actual == len(payload)

    @pytest.mark.contract
    def test_wrong_contract_id_is_rejected(self, valid_pcm: bytes) -> None:
        with pytest.raises(ContractViolation) as excinfo:
            validate_window_request(
                pcm_window=valid_pcm, contract_id="raw-waveform-v2", sample_rate_hz=SAMPLE_RATE_HZ
            )
        assert excinfo.value.code == "PROTO_CONTRACT_ID"

    @pytest.mark.contract
    @pytest.mark.parametrize("rate", [8_000, 16_001, 44_100, 48_000, 0])
    def test_wrong_sample_rate_is_rejected(self, valid_pcm: bytes, rate: int) -> None:
        with pytest.raises(ContractViolation) as excinfo:
            validate_window_request(
                pcm_window=valid_pcm, contract_id=CONTRACT_ID, sample_rate_hz=rate
            )
        assert excinfo.value.code == "PROTO_SAMPLE_RATE"

    @pytest.mark.privacy
    def test_rejection_messages_never_echo_the_input(self) -> None:
        """Prevents a caller-supplied string reaching the Gateway's logs through a gRPC status detail.

        The Scorer's rejection detail travels to the Gateway, into its structured log, and from there to
        CloudWatch. A message interpolated from the request would carry a contract_id — or, with one
        careless f-string, a payload fragment — the whole way (rules.md R-17). So the detail strings are
        module-level constants and this test asserts they contain no format placeholder of any kind.
        """
        details = [
            contract.WRONG_WINDOW_SIZE,
            contract.WRONG_CONTRACT_ID,
            contract.WRONG_SAMPLE_RATE,
        ]
        for detail in details:
            assert "{" not in detail and "%" not in detail
            assert detail == detail.format()  # a no-arg format() would raise on a placeholder

        hostile = "raw-waveform-v1\nsecret-looking-value-from-a-caller"
        with pytest.raises(ContractViolation) as excinfo:
            validate_window_request(
                pcm_window=b"\x00" * WINDOW_BYTES,
                contract_id=hostile,
                sample_rate_hz=SAMPLE_RATE_HZ,
            )
        assert hostile not in excinfo.value.detail
        assert "secret-looking-value-from-a-caller" not in str(excinfo.value)


class TestConversionShape:
    """Shape and dtype are what ORT would complain about; the complaint would not name the contract."""

    def test_shape_and_dtype(self, valid_pcm: bytes) -> None:
        window = pcm16_to_float32(valid_pcm)
        assert window.shape == ONNX_INPUT_SHAPE == (1, 40_960)
        assert window.dtype == np.float32

    def test_conversion_refuses_a_wrong_length_buffer(self) -> None:
        """Prevents np.frombuffer silently reinterpreting a short buffer at the wrong sample count.

        ``pcm16_to_float32`` is reachable from fixture builders and future batch entry points that never
        went through ``validate_window_request``. Without this check a 40,960-byte buffer would become a
        20,480-sample array and the reshape would raise something about sizes, not about the contract.
        """
        with pytest.raises(ContractViolation) as excinfo:
            pcm16_to_float32(b"\x00" * (WINDOW_BYTES - 2))
        assert excinfo.value.code == "PROTO_WINDOW_SIZE"

    def test_little_endian_is_enforced_by_the_dtype(self) -> None:
        """Prevents a big-endian read producing a plausible but completely different waveform.

        The two interpretations of the same bytes both yield valid int16 in range, so nothing downstream
        would notice. 0x0100 little-endian is 1; big-endian it is 256.
        """
        payload = b"\x01\x00" + b"\x00" * (WINDOW_BYTES - 2)
        window = pcm16_to_float32(payload)
        assert window[0, 0] == np.float32(1) / np.float32(32_768.0)

    def test_round_trip_is_exact(self, valid_pcm: bytes) -> None:
        """Prevents a lossy inverse silently changing the committed parity fixture on regeneration.

        Exact because 32768.0 is a power of two: the forward divide and the inverse multiply are both
        lossless in float32 across the int16 range. If the divisor were ever changed to a non-power of
        two, this is the test that would fail first.
        """
        assert float32_to_pcm16(pcm16_to_float32(valid_pcm)) == valid_pcm


# -- cross-service parity ---------------------------------------------------------------------------

_ASSIGNMENT = re.compile(
    r"^(?P<name>[A-Z][A-Z0-9_]*)\s*:\s*Final\[[^\]]+\]\s*=\s*(?P<value>[^#\n]+)",
    re.MULTILINE,
)

#: The constants that MUST be identical in both services. WS_FRAME_BYTES (648) and
#: BYTES_PER_FRAME_PAYLOAD (640) are deliberately excluded: the Scorer never sees a WebSocket frame, so
#: declaring them here would create a third copy of two numbers this process cannot exercise.
_SHARED_CONSTANTS = (
    "CONTRACT_ID",
    "SAMPLE_RATE_HZ",
    "CHANNELS",
    "PCM_DTYPE",
    "WINDOW_MS",
    "WINDOW_SAMPLES",
    "WINDOW_BYTES",
    "ONNX_INPUT_BATCH",
    "ONNX_INPUT_SAMPLES",
    "PCM16_FLOAT_DIVISOR",
)


def _parse_python_constants(path: Path) -> dict[str, object]:
    """Extract simple ``NAME: Final[...] = literal`` assignments by TEXT, without importing.

    One level of same-module reference is resolved (``ONNX_INPUT_SAMPLES: Final[int] = WINDOW_SAMPLES``),
    because that is how ``gateway/app/constants.py`` derives the ONNX shape from the window size — and
    deriving it there rather than repeating the number is the behaviour rules.md R-23 asks for. A parser
    that only understood literals would have quietly dropped the derived name, and this class's
    guard test would then be the only thing standing between that and a vacuous parity check.
    """
    found: dict[str, object] = {}
    references: dict[str, str] = {}
    for match in _ASSIGNMENT.finditer(path.read_text(encoding="utf-8")):
        name = match.group("name")
        raw = match.group("value").strip()
        if raw.startswith(('"', "'")):
            found[name] = raw.strip("\"'")
        elif re.fullmatch(r"-?[\d_]+", raw):
            found[name] = int(raw.replace("_", ""))
        elif re.fullmatch(r"-?[\d_]*\.[\d_]*(e-?\d+)?", raw, re.IGNORECASE):
            found[name] = float(raw.replace("_", ""))
        elif re.fullmatch(r"[A-Z][A-Z0-9_]*", raw):
            references[name] = raw
    for name, target in references.items():
        if target in found:
            found[name] = found[target]
    return found


@pytest.mark.parity
class TestGatewayConstantsParity:
    """The Scorer's copy of the wire contract must equal the Gateway's (rules.md R-23).

    ``gateway/app/constants.py`` is the single Python definition, and importing it would be strictly
    better. It is not possible: the Scorer image contains no ``gateway/`` tree (see ``scorer/Dockerfile``)
    and the two services are separate deployables with separate ECR repositories. Copying the module in
    at build time would create a second file with the same authority.

    So the values are pinned once per service and equality is asserted here by parsing the Gateway's
    module as TEXT — the same technique ``gateway/tests/test_constants_parity.py`` already uses against
    ``pwa/src/lib/constants.ts``, because a browser cannot import Python either. Parsing rather than
    importing is what keeps this test runnable inside a Scorer-only checkout and inside CI jobs that do
    not install the Gateway's dependencies.

    What this prevents: a window-size change landing in one service. The Gateway would assemble 40,960
    samples and the Scorer would validate against a different number, so every request would be
    rejected — or, far worse, the reverse, where both accept and the model is fed a differently-shaped
    world with no error anywhere.
    """

    def test_gateway_constants_module_is_parseable(self) -> None:
        """Prevents this whole class silently passing because the regex stopped matching anything."""
        gateway = _parse_python_constants(REPO_ROOT / "gateway" / "app" / "constants.py")
        missing = [name for name in _SHARED_CONSTANTS if name not in gateway]
        assert not missing, f"could not parse from gateway/app/constants.py: {missing}"

    @pytest.mark.parametrize("name", _SHARED_CONSTANTS)
    def test_constant_matches_the_gateway(self, name: str) -> None:
        gateway = _parse_python_constants(REPO_ROOT / "gateway" / "app" / "constants.py")
        assert getattr(contract, name) == gateway[name], (
            f"{name} differs between scorer/app/contract.py and gateway/app/constants.py"
        )

    def test_scorer_does_not_redeclare_frame_level_constants(self) -> None:
        """Prevents a third copy of 648/640 appearing in a service that cannot exercise them.

        An unexercised copy of a constant is one that drifts without any test noticing.
        """
        assert not hasattr(contract, "WS_FRAME_BYTES")
        assert not hasattr(contract, "BYTES_PER_FRAME_PAYLOAD")

    def test_arithmetic_self_check_runs_at_import(self) -> None:
        """Prevents the parity test being the ONLY guard, which a Scorer-only checkout would skip."""
        contract._self_check()


@pytest.mark.parity
class TestContractVectorFixture:
    """``ml/fixtures/contract_vector_v1.npy`` is re-scored at every startup (frame_contract.md §6)."""

    def test_fixture_matches_a_fresh_generation_bit_for_bit(
        self, contract_vector: np.ndarray
    ) -> None:
        """Prevents the committed parity fixture drifting from the generator that documents it.

        Bit-exact, not ``allclose``. A tolerance would let a 32767.0 divisor through: the relative
        difference is 3e-5, comfortably inside any tolerance someone would pick by eye — and comfortably
        outside the atol=1e-4 the ONNX parity gate applies to the resulting raw score.
        """
        sys.path.insert(0, str(REPO_ROOT / "ml" / "fixtures"))
        try:
            import make_contract_vector
        finally:
            sys.path.pop(0)
        assert np.array_equal(contract_vector, make_contract_vector.build_contract_vector())

    def test_fixture_matches_the_model_input_contract(self, contract_vector: np.ndarray) -> None:
        assert contract_vector.shape == ONNX_INPUT_SHAPE
        assert contract_vector.dtype == np.float32

    def test_fixture_exercises_both_int16_rails(self, contract_vector: np.ndarray) -> None:
        """Prevents a fixture that cannot distinguish the two divisors from being the parity vector.

        The rails are the only sample values at which 32767.0 and 32768.0 differ by more than a rounding
        wobble. A fixture without them would score almost identically under either, so the every-startup
        parity check would pass on a mis-scaled build.
        """
        assert contract_vector.min() == -1.0
        assert contract_vector.max() == np.float32(32_767) / np.float32(32_768.0)

    def test_fixture_is_reachable_as_exact_request_bytes(self, contract_vector: np.ndarray) -> None:
        """Prevents the parity vector being unusable as a real ScoreWindow payload.

        The fixture is stored as float32 (frame_contract.md §6) but the RPC carries int16 bytes. If the
        inverse were not exact, a request built from this fixture would score differently from the
        startup check that uses the array directly, and the two parity gates would disagree.
        """
        payload = float32_to_pcm16(contract_vector)
        assert len(payload) == WINDOW_BYTES
        assert np.array_equal(pcm16_to_float32(payload), contract_vector)

    def test_fixture_is_committed_at_the_documented_path(self) -> None:
        assert CONTRACT_VECTOR_PATH.is_file()
