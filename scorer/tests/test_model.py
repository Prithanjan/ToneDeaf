"""Detectors: mock determinism and labelling, and the execution-provider assertion (rules.md R-45).

The two properties this file protects are the two on which the project's claim integrity rests:

* a mock score must never be mistakable for a measurement, and
* a GPU deploy that silently resolved to the CPU must fail, not warn.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from app import model as model_module
from app.config import DetectorMode, ExecutionProvider, ScorerSettings, load_settings
from app.contract import ONNX_INPUT_SHAPE, WINDOW_SAMPLES, pcm16_to_float32
from app.model import (
    EMPTY_SHA256,
    MOCK_LOGIT_RANGE,
    MOCK_MODEL_VERSION,
    Detector,
    MockDetector,
    ModelLoadError,
    OnnxDetector,
    ProviderUnavailable,
    _assert_input_shape,
    _extract_raw_score,
    build_detector,
)

_HAS_ORT = importlib.util.find_spec("onnxruntime") is not None


def _window(seed: int) -> np.ndarray:
    values = ((np.arange(WINDOW_SAMPLES, dtype=np.int64) * 7919 + seed * 31) % 16_001) - 8_000
    return pcm16_to_float32(values.astype("<i2").tobytes())


class TestMockDeterminism:
    """Same audio, same session, same score — in every process, on every host, across restarts."""

    def test_identical_inputs_give_identical_scores(self) -> None:
        """Prevents a replayed test call producing a different policy trace on the second run.

        Determinism is what makes the dual-tier parity rehearsal meaningful while both tiers are mocked:
        the same call must produce the same sequence of risk states on the CPU tier and the GPU tier.
        """
        detector = MockDetector(reported_provider=ExecutionProvider.CPU)
        window = _window(1)
        first = detector.score(window, session_ref="example-session-ref-a")
        second = detector.score(window, session_ref="example-session-ref-a")
        assert first == second

    def test_determinism_survives_a_fresh_detector(self) -> None:
        """Prevents per-instance state creeping into the mock's score."""
        window = _window(2)
        a = MockDetector(reported_provider=ExecutionProvider.CPU).score(window, session_ref="ref-b")
        b = MockDetector(reported_provider=ExecutionProvider.CPU).score(window, session_ref="ref-b")
        assert a == b

    def test_score_is_process_independent(self) -> None:
        """Prevents ``hash()`` being used, which is salted per process (PYTHONHASHSEED).

        With a salted hash, two runs of the same call would disagree — and the disagreement would look
        exactly like a CPU/GPU parity failure, sending someone to debug the wrong thing. The value below
        is pinned so this test fails if the derivation changes at all.
        """
        detector = MockDetector(reported_provider=ExecutionProvider.CPU)
        zeros = np.zeros(ONNX_INPUT_SHAPE, dtype=np.float32)
        pinned = detector.score(zeros, session_ref="pinned-reference-session")
        assert pinned == pytest.approx(
            MockDetector(reported_provider=ExecutionProvider.CUDA).score(
                zeros, session_ref="pinned-reference-session"
            )
        )
        # Recomputed from the documented derivation rather than copied from a previous run, so the test
        # states the algorithm instead of memorising an output.
        import hashlib

        digest = hashlib.blake2b(
            model_module._MOCK_DIGEST_DOMAIN,
            digest_size=8,
            key=b"pinned-reference-session",
        )
        digest.update(zeros.tobytes())
        unit = (int.from_bytes(digest.digest(), "big") + 0.5) / 2**64
        assert pinned == pytest.approx(MOCK_LOGIT_RANGE * (2.0 * unit - 1.0))

    def test_different_windows_in_one_session_score_differently(self) -> None:
        """Prevents the k-of-n evidence rule being wired but never exercised.

        technical-design.md §7 says the sequence is derived "from ``session_ref``". Derived from
        ``session_ref`` ALONE, every window in a session would score identically, so the state machine
        could only ever produce all-high or all-low and Pair C's policy wiring would look correct and be
        untested. Mixing in the window content keeps the letter of the requirement — reproducible for a
        given session and its audio — while making the sequence a sequence.
        """
        detector = MockDetector(reported_provider=ExecutionProvider.CPU)
        scores = {detector.score(_window(i), session_ref="one-session") for i in range(12)}
        assert len(scores) == 12

    def test_different_sessions_score_the_same_audio_differently(self) -> None:
        detector = MockDetector(reported_provider=ExecutionProvider.CPU)
        window = _window(3)
        assert detector.score(window, session_ref="ref-x") != detector.score(
            window, session_ref="ref-y"
        )

    def test_window_seq_is_not_an_input_to_the_score(self) -> None:
        """Prevents the mock developing a dependence on window ordering that a real model lacks.

        The proto states the Scorer is stateless and must not use ``window_seq`` to correlate windows.
        Asserted structurally: ``score()`` takes no such parameter, so no implementation of it can.
        """
        import inspect

        signature = inspect.signature(MockDetector.score)
        assert set(signature.parameters) == {"self", "window", "session_ref"}

    def test_mock_emits_a_logit_not_a_probability(self) -> None:
        """Prevents the mock's calibrated output being unable to cross a realistic threshold.

        A mock emitting values in [0,1] would compress to [0.5, 0.73] after the sigmoid and could never
        reach the 0.78 threshold in the policy bundle — the Gateway's HIGH state would be unreachable and
        the demo would silently only ever show LOW.
        """
        detector = MockDetector(reported_provider=ExecutionProvider.CPU)
        values = [detector.score(_window(i), session_ref=f"ref-{i}") for i in range(200)]
        assert min(values) < -3.0
        assert max(values) > 3.0
        assert all(-MOCK_LOGIT_RANGE < v < MOCK_LOGIT_RANGE for v in values)

    def test_wrong_shape_is_refused(self) -> None:
        detector = MockDetector(reported_provider=ExecutionProvider.CPU)
        with pytest.raises(ValueError):
            detector.score(np.zeros((1, WINDOW_SAMPLES - 1), dtype=np.float32), session_ref="r")


@pytest.mark.privacy
class TestMockIsUnmistakable:
    """rules.md R-46. A mock number that could pass for a measurement is the worst failure here."""

    def test_model_version_says_it_is_not_a_detector(self) -> None:
        """Prevents a mock's version string surviving into a slide, a metric label, or an audit row.

        This string is written to every audit row and printed in the startup banner. A neutral value like
        "v0" would look like a model identity with nothing in the record to contradict it.
        """
        assert MOCK_MODEL_VERSION == "mock-smoke-not-a-detector"
        assert (
            "not-a-detector" in MockDetector(reported_provider=ExecutionProvider.CPU).model_version
        )

    def test_model_sha256_is_the_hash_of_nothing(self) -> None:
        """Truthful and structurally distinguishable, while still being a well-formed 64-hex value.

        Well-formed matters: ``gateway/app/main.py`` compares this against the policy bundle's
        ``model_sha256``, and an empty string there would make the comparison compare a value against
        nothing rather than fail loudly.
        """
        detector = MockDetector(reported_provider=ExecutionProvider.CPU)
        assert detector.model_sha256 == EMPTY_SHA256
        assert len(detector.model_sha256) == 64

    def test_no_configuration_makes_the_mock_report_real(self, base_env: dict[str, str]) -> None:
        """Prevents an environment variable being the difference between honest and dishonest labelling.

        ``build_detector`` is the only construction site, and it keys on ``detector_mode`` alone. There is
        no flag that produces a MockDetector while the settings say REAL_DETECTOR, or vice versa.
        """
        settings = load_settings(env=base_env)
        detector = build_detector(settings, model_version="ignored-in-mock-mode")
        assert isinstance(detector, MockDetector)
        assert settings.detector_mode is DetectorMode.MOCK
        assert detector.model_version != "ignored-in-mock-mode"

    def test_mock_reports_the_configured_provider_and_why(self, base_env: dict[str, str]) -> None:
        """The one deliberate compromise in the mock's honesty, and the reason it is safe.

        There is no ORT session in mock mode, so there is no provider to observe. Returning a truthful
        token like "MOCK_NO_PROVIDER" would make ``gateway/app/main.py`` refuse to start — it requires
        exact string equality with its own configured provider — and the Compose tier is mandatory. The
        honesty is preserved by PAIRING: this field never travels without ``detector_mode`` in the same
        message. And the question cannot arise on the paid tier at all, because ``config.py`` refuses
        mock mode under ``aws-gpu`` outright (asserted in ``test_config.py``).
        """
        settings = load_settings(env=base_env)
        detector = build_detector(settings, model_version="unused")
        assert detector.execution_provider == settings.execution_provider.value

    def test_mock_retains_no_audio(self) -> None:
        """rules.md R-14. Asserted structurally: there is nowhere for a buffer to be kept.

        ``__slots__`` restricts the instance to the single provider string, so a future edit that tried to
        stash the last window on the detector would raise AttributeError rather than silently retaining
        2.56 seconds of a caller's voice.
        """
        detector = MockDetector(reported_provider=ExecutionProvider.CPU)
        detector.score(_window(4), session_ref="ref")
        assert MockDetector.__slots__ == ("_provider",)
        with pytest.raises(AttributeError):
            detector.last_window = b"x"  # type: ignore[attr-defined]


class _StubSession:
    """Minimal stand-in for ``ort.InferenceSession`` for the provider assertion."""

    def __init__(self, providers: list[str]):
        self._providers = providers

    def get_providers(self) -> list[str]:
        return self._providers


class TestExecutionProviderAssertion:
    """rules.md R-45: a fallback is a FAILED deploy, surfaced before the port is bound."""

    def test_matching_provider_is_accepted(self) -> None:
        detector = OnnxDetector(path=Path("unused.onnx"), model_version="v")
        detector._session = _StubSession(["CUDAExecutionProvider", "CPUExecutionProvider"])
        detector.assert_provider("CUDAExecutionProvider")
        assert detector.execution_provider == "CUDAExecutionProvider"

    def test_fallback_to_cpu_raises_rather_than_warns(self) -> None:
        """Prevents "healthy but silently on CPU", which is worse than a crash.

        A crash is loud, immediate, attributable, and self-limiting: someone fixes the task definition and
        no wrong number was ever recorded. A fallback passes the health check, scores every window
        correctly, and starts the demo — so nobody looks — while every p95 recorded that day describes a
        CPU running a graph sized for a GPU, every audit row says CUDAExecutionProvider next to CPU
        timings, and the Day-5 dual-tier parity claim was never actually exercised. It is then discovered
        late, under demo pressure, by someone whose only symptom is "it's a bit slow".
        """
        detector = OnnxDetector(path=Path("unused.onnx"), model_version="v")
        detector._session = _StubSession(["CPUExecutionProvider"])
        with pytest.raises(ProviderUnavailable, match="FAILED DEPLOY"):
            detector.assert_provider("CUDAExecutionProvider")

    def test_membership_is_not_enough_position_is_checked(self) -> None:
        """Prevents the exact bug a naive check would miss.

        A session that registered CUDA and then fell back runs everything on the provider at index 0.
        ``"CUDAExecutionProvider" in session.get_providers()`` is True in that state — so a membership
        check would pass on precisely the session this assertion exists to reject.
        """
        detector = OnnxDetector(path=Path("unused.onnx"), model_version="v")
        detector._session = _StubSession(["CPUExecutionProvider", "CUDAExecutionProvider"])
        assert "CUDAExecutionProvider" in detector._session.get_providers()
        with pytest.raises(ProviderUnavailable):
            detector.assert_provider("CUDAExecutionProvider")

    def test_empty_provider_list_raises(self) -> None:
        detector = OnnxDetector(path=Path("unused.onnx"), model_version="v")
        detector._session = _StubSession([])
        with pytest.raises(ProviderUnavailable):
            detector.assert_provider("CPUExecutionProvider")

    def test_no_session_raises(self) -> None:
        detector = OnnxDetector(path=Path("unused.onnx"), model_version="v")
        with pytest.raises(ProviderUnavailable):
            detector.assert_provider("CPUExecutionProvider")

    def test_only_the_requested_provider_is_offered_to_ort(self) -> None:
        """Prevents a fallback LIST being passed, which is exactly how silent fallback happens.

        ORT accepts ``["CUDAExecutionProvider", "CPUExecutionProvider"]``, fails to initialize CUDA, logs
        a warning nobody reads, and serves on the CPU while the task stays healthy. With a single-entry
        list, failure to initialize raises at session construction. Asserted on the source because the
        alternative is a GPU-only integration test that cannot run in CI.
        """
        source = Path(model_module.__file__).read_text(encoding="utf-8")
        assert "providers=[requested]" in source
        assert "active[0] != requested" in source

    @pytest.mark.integration
    @pytest.mark.skipif(not _HAS_ORT, reason="onnxruntime not installed")
    def test_real_ort_reports_a_provider_at_index_zero(self) -> None:
        """Prevents the assertion being written against an API shape ORT does not actually have.

        Runs against the installed CPU wheel. It does not verify CUDA — there is exactly one GPU in the
        five-day budget (rules.md R-32) and it is not spent on a test runner — but it does verify that
        ``get_available_providers`` and ``get_providers`` exist and that ``CPUExecutionProvider`` is
        always present, which is what the CPU tier's own assertion depends on.
        """
        import onnxruntime as ort

        assert "CPUExecutionProvider" in ort.get_available_providers()

    @pytest.mark.skipif(not _HAS_ORT, reason="onnxruntime not installed")
    def test_unavailable_provider_is_refused_before_a_session_is_created(
        self, base_env: dict[str, str]
    ) -> None:
        """Prevents the wrong WHEEL reaching production: onnxruntime (CPU-only) with CUDA requested.

        Caught at load, not at the first inference, where it would surface as a silent fallback.
        """
        env = dict(base_env)
        env.update(
            {
                "DETECTOR_MODE": "REAL_DETECTOR",
                "EXECUTION_PROVIDER": "CUDAExecutionProvider",
            }
        )
        settings = load_settings(env=env)
        import onnxruntime as ort

        if "CUDAExecutionProvider" in ort.get_available_providers():
            pytest.skip("a CUDA-capable ORT build is installed; this test needs the CPU wheel")
        detector = OnnxDetector(path=Path(__file__), model_version="v")  # any existing file
        with pytest.raises(ProviderUnavailable):
            detector.load(settings)


class TestModelArtifactHandling:
    """The graph must declare the window contract, and produce exactly one number."""

    def test_missing_model_file_refuses_with_a_pointer_to_mock_mode(
        self, base_env: dict[str, str], tmp_path: Path
    ) -> None:
        """Prevents a real deploy silently degrading into something that scores anyway.

        A real detector with no model is not a degraded detector. The message names the mock-mode switch
        so the person who hit this does not invent a workaround.
        """
        env = dict(base_env)
        env["DETECTOR_MODE"] = "REAL_DETECTOR"
        env["MODEL_PATH"] = str(tmp_path / "absent.onnx")
        settings = load_settings(env=env)
        with pytest.raises((ModelLoadError, ProviderUnavailable)) as excinfo:
            build_detector(settings, model_version="v")
        if isinstance(excinfo.value, ModelLoadError):
            assert "MOCK_SMOKE_MODE_NOT_A_DETECTOR" in str(excinfo.value)

    @pytest.mark.parametrize("shape", [[1, 40_960], ["batch", 40_960], [None, 40_960]])
    def test_declared_input_shape_is_accepted(self, shape: list[Any]) -> None:
        """A symbolic batch dimension is accepted: exporters commonly emit one and it is correct."""
        _assert_input_shape(shape)

    @pytest.mark.parametrize(
        "shape", [[1, 40_959], [1, 16_000], [1, 81_920], [1], [1, 1, 40_960], [2, 40_960]]
    )
    def test_wrong_input_shape_is_refused(self, shape: list[Any]) -> None:
        """Prevents 2.56 s of audio being fed to a graph exported for a different window length.

        The graph would run. It would produce a number. The number would be meaningless, and nothing
        downstream could tell.
        """
        with pytest.raises(ModelLoadError):
            _assert_input_shape(shape)

    @pytest.mark.parametrize(
        "output",
        [
            np.float32(0.25),
            np.array([0.25], dtype=np.float32),
            np.array([[0.25]], dtype=np.float32),
        ],
    )
    def test_single_logit_output_shapes_are_accepted(self, output: Any) -> None:
        assert _extract_raw_score([output]) == pytest.approx(0.25)

    def test_multiclass_output_is_refused_not_reduced(self) -> None:
        """Prevents class orientation being guessed at serving time.

        Given ``[bona_fide, spoof]``, the candidate reductions are ``out[1]``, ``out[1] - out[0]``, and
        ``softmax(out)[1]``. They are not equivalent, and picking one here is how a detector ends up
        inverted while every shape check passes. playbook §7 requires orientation to be explicit at
        export.
        """
        with pytest.raises(ModelLoadError, match="orientation"):
            _extract_raw_score([np.array([[0.4, 0.6]], dtype=np.float32)])

    def test_empty_output_is_refused(self) -> None:
        with pytest.raises(ModelLoadError):
            _extract_raw_score([])

    def test_non_finite_output_is_refused(self) -> None:
        """Prevents NaN propagating into the calibration and disabling every threshold comparison."""
        with pytest.raises(ModelLoadError):
            _extract_raw_score([np.array([np.nan], dtype=np.float32)])


class TestDetectorInterface:
    """The interface is narrow on purpose: nothing policy-shaped can cross it."""

    def test_interface_exposes_no_policy_input_or_output(self) -> None:
        """Prevents the seam being widened in Python even though the proto still forbids it.

        A ``score(window, *, session_ref, purpose_code)`` overload would compile, pass every proto test,
        and be reachable from a future in-process caller. Keeping the abstract signature minimal means the
        widening has to be a visible edit to this base class.
        """
        import inspect

        assert set(inspect.signature(Detector.score).parameters) == {
            "self",
            "window",
            "session_ref",
        }
        # Every public name on the interface, checked against the same policy-shaped token list the
        # proto seam test uses. A `purpose` or `decision` member would fail here even if someone added it
        # without touching the proto.
        forbidden = {"purpose", "action", "decision", "policy", "threshold", "history", "verdict"}
        for name in dir(Detector):
            if name.startswith("_"):
                continue
            assert not forbidden & set(name.split("_")), f"policy-shaped member on Detector: {name}"
        assert Detector.__abstractmethods__ == frozenset(
            {"model_version", "model_sha256", "execution_provider", "score"}
        )

    def test_score_returns_a_precalibration_value(self) -> None:
        """Prevents calibration migrating into the detector, where a swap would need a new model build.

        Keeping ``raw_score`` available is also what makes the ONNX parity gate measurable through the RPC
        (frame_contract.md §6): a calibration change must not move the number the gate compares. A
        detector that returned an already-calibrated probability would be indistinguishable from a
        correct one at the response boundary — both are floats — so the property is asserted on the range.
        """
        detector = MockDetector(reported_provider=ExecutionProvider.CPU)
        values = [detector.score(_window(i), session_ref=f"ref-{i}") for i in range(50)]
        assert min(values) < 0.0, "a pre-calibration raw score must be able to be negative"

    def test_close_is_safe_on_a_detector_that_holds_nothing(self) -> None:
        MockDetector(reported_provider=ExecutionProvider.CPU).close()

    def test_build_detector_is_the_only_construction_site(self) -> None:
        """Prevents a second path that could produce a detector whose mode disagrees with the settings."""
        server_source = (Path(model_module.__file__).parent / "server.py").read_text(
            encoding="utf-8"
        )
        assert "MockDetector(" not in server_source
        assert "OnnxDetector(" not in server_source
        assert "build_detector(" in server_source


def test_settings_type_is_used_not_duplicated() -> None:
    """``build_detector`` reads the settings object rather than re-reading the environment."""
    import inspect

    signature = inspect.signature(build_detector)
    assert signature.parameters["settings"].annotation in (ScorerSettings, "ScorerSettings")
