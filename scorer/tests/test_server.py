"""The servicer: quality assessment, eligibility, static rejections, and the health/parity contract.

The servicer is exercised directly rather than over a socket. A real channel would test grpcio, which is
pinned and not ours; what needs testing is that a malformed request aborts with a STATIC detail, that an
ineligible window is skipped rather than counted low, and that the health response carries the parity set
the Gateway refuses to start without.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

import grpc
import numpy as np
import pytest

from app import server as server_module
from app import voice_scorer_pb2 as pb
from app.calibration import (
    Calibration,
    CalibrationError,
    load_calibration,
    placeholder_calibration,
)
from app.config import ArtifactState, ScorerSettings, load_settings
from app.contract import (
    CONTRACT_ID,
    SAMPLE_RATE_HZ,
    WINDOW_BYTES,
    WINDOW_SAMPLES,
    float32_to_pcm16,
    pcm16_to_float32,
)
from app.model import ModelLoadError, build_detector
from app.server import (
    DISQUALIFYING_FLAGS,
    ScorerRuntime,
    VoiceScorerServicer,
    _check_contract_vector,
    _supported_artifact_state,
    _verify_model_pairing,
    assess_window,
    build_runtime,
    create_server,
)
from tests.conftest import EXAMPLE_MODEL_SHA256


class _Aborted(Exception):
    """Raised by the fake context so a test can inspect the code and detail ``abort`` was given."""

    def __init__(self, code: grpc.StatusCode, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


class _FakeContext:
    """Stands in for ``grpc.ServicerContext``. ``abort`` raises, exactly as the real one does."""

    def abort(self, code: grpc.StatusCode, detail: str) -> NoReturn:
        raise _Aborted(code, detail)


class _FakeRpcError(grpc.RpcError):
    """``grpc.RpcError`` alone has no ``code()``; the real failure path gets it from the Call mixin."""

    def code(self) -> grpc.StatusCode:
        return grpc.StatusCode.UNAVAILABLE


@pytest.fixture
def runtime(mock_settings: ScorerSettings) -> ScorerRuntime:
    """A mock-mode runtime with the built-in placeholder calibration — the Phase-1 shape."""
    detector = build_detector(mock_settings, model_version="unused")
    return ScorerRuntime(
        settings=mock_settings,
        detector=detector,
        calibration=placeholder_calibration(),
        artifact_state=ArtifactState.RESEARCH_ONLY,
        contract_vector_parity_ok=False,
        proto_sha256="0" * 64,
    )


@pytest.fixture
def servicer(runtime: ScorerRuntime) -> VoiceScorerServicer:
    return VoiceScorerServicer(runtime)


def _request(pcm: bytes, **overrides: Any) -> Any:
    fields: dict[str, Any] = {
        "pcm_window": pcm,
        "contract_id": CONTRACT_ID,
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "window_seq": 7,
        "session_ref": "example-pseudonymous-call-reference",
    }
    fields.update(overrides)
    return pb.ScoreWindowRequest(**fields)


class TestQualityAssessment:
    """Observations about the WINDOW, never about the speaker (rules.md R-41)."""

    def test_silence_is_low_energy_and_ineligible(self, silent_window: np.ndarray) -> None:
        """rules.md R-09 and playbook §1: do not infer from silence.

        The important half is ``eligible = False``, not the flag. An ineligible window must be SKIPPED by
        the k-of-n counter, never counted as a low-risk observation — otherwise a caller who goes quiet
        accumulates evidence of being genuine, and the cheapest way to look genuine becomes saying nothing.
        """
        assessment = assess_window(silent_window)
        assert "LOW_ENERGY" in assessment.flags
        assert assessment.eligible is False

    def test_normal_speech_level_window_is_eligible(self, valid_pcm: bytes) -> None:
        assessment = assess_window(pcm16_to_float32(valid_pcm))
        assert assessment.eligible is True
        assert "LOW_ENERGY" not in assessment.flags

    def test_clipping_is_reported_but_not_disqualifying(self) -> None:
        """Prevents handing an attacker a one-line evidence-suppression channel.

        If clipping made a window ineligible, an attacker could switch the detector off by overdriving
        their own input — far cheaper than defeating the model. So clipping is REPORTED, so a reviewer can
        see the channel was bad, and the window is still scored and still counted.
        """
        samples = np.full(WINDOW_SAMPLES, 32_767, dtype="<i2")
        samples[1::2] = -32_768
        assessment = assess_window(pcm16_to_float32(samples.tobytes()))
        assert "CLIPPING_DETECTED" in assessment.flags
        assert assessment.eligible is True

    def test_dc_offset_is_reported_but_not_disqualifying(self) -> None:
        """Same reasoning as clipping: a capture defect must not become a suppression channel."""
        samples = np.full(WINDOW_SAMPLES, 4_000, dtype="<i2")
        samples[1::2] = 2_000
        assessment = assess_window(pcm16_to_float32(samples.tobytes()))
        assert "DC_OFFSET" in assessment.flags
        assert assessment.eligible is True

    def test_disqualifying_set_is_deliberately_minimal(self) -> None:
        """Prevents the disqualifying list growing, since every entry is an attack surface.

        Each disqualifying flag is a way to make evidence stop accumulating by degrading the audio in a
        specific, reproducible way. ``LOW_ENERGY`` is on the list because an attacker who goes quiet
        supplies no synthetic speech to detect — there is nothing to suppress.
        """
        assert DISQUALIFYING_FLAGS == frozenset({"LOW_ENERGY", "INSUFFICIENT_VOICED"})

    def test_narrowband_is_not_computed(self) -> None:
        """rules.md R-12 and R-39. Prevents a hard-coded spectral cutoff entering the serving path.

        Estimating bandwidth needs a spectral transform, and a frequency boundary in this file is exactly
        the hard-coded cutoff R-12 forbids — a codec observation dressed up as spoof evidence. The proto
        keeps ``NARROWBAND_SUSPECTED`` for the ablation-gated diagnostics plane; nothing here emits it.
        """
        assessment = assess_window(pcm16_to_float32(b"\x00\x10" * WINDOW_SAMPLES))
        assert "NARROWBAND_SUSPECTED" not in assessment.flags
        source = (Path(__file__).parents[1] / "app" / "server.py").read_text(encoding="utf-8")
        assert "NARROWBAND_SUSPECTED" in source  # explained in a docstring, never emitted
        assert "np.fft" not in source and "rfft" not in source

    def test_flags_never_move_the_score(
        self, servicer: VoiceScorerServicer, valid_pcm: bytes
    ) -> None:
        """rules.md R-09. Prevents quality becoming a second, undocumented detector.

        A flag decides whether a window COUNTS. It must never decide which way it counts. Asserted by
        scoring the same audio and confirming the risk is a pure function of the detector and calibration.
        """
        response = servicer.ScoreWindow(_request(valid_pcm), _FakeContext())
        raw = servicer._runtime.detector.score(
            pcm16_to_float32(valid_pcm), session_ref="example-pseudonymous-call-reference"
        )
        expected = servicer._runtime.calibration.apply(raw)
        assert response.spoof_risk == pytest.approx(expected, abs=1e-6)

    def test_wrong_shape_is_refused(self) -> None:
        with pytest.raises(ValueError):
            assess_window(np.zeros((1, WINDOW_SAMPLES - 1), dtype=np.float32))


class TestScoreWindow:
    """One window in, one calibrated number plus provenance out."""

    def test_response_carries_the_full_provenance(
        self, servicer: VoiceScorerServicer, valid_pcm: bytes
    ) -> None:
        """Prevents a score travelling without the labels that qualify it (rules.md R-46).

        ``model_version``, ``calibration_version``, and ``detector_mode`` are on every single response, not
        only on health. A score in a log or an audit row must be self-describing: whoever reads it later
        will not have the health response next to it.
        """
        response = servicer.ScoreWindow(_request(valid_pcm), _FakeContext())
        assert 0.0 <= response.spoof_risk <= 1.0
        assert response.model_version == "mock-smoke-not-a-detector"
        assert response.calibration_version == "0.0.0-placeholder"
        assert pb.DetectorMode.Name(response.detector_mode) == "MOCK_SMOKE_MODE_NOT_A_DETECTOR"
        assert response.scorer_latency_us >= 0

    def test_mock_response_is_labelled_on_every_call(
        self, servicer: VoiceScorerServicer, valid_pcm: bytes
    ) -> None:
        """rules.md R-46. The label is per-response, so no single score can be quoted without it."""
        for seq in range(5):
            response = servicer.ScoreWindow(_request(valid_pcm, window_seq=seq), _FakeContext())
            assert response.detector_mode == pb.DetectorMode.Value("MOCK_SMOKE_MODE_NOT_A_DETECTOR")

    def test_identical_requests_produce_identical_responses(
        self, servicer: VoiceScorerServicer, valid_pcm: bytes
    ) -> None:
        first = servicer.ScoreWindow(_request(valid_pcm), _FakeContext())
        second = servicer.ScoreWindow(_request(valid_pcm), _FakeContext())
        assert first.spoof_risk == second.spoof_risk
        assert first.raw_score == second.raw_score

    def test_raw_and_calibrated_scores_are_both_returned(
        self, servicer: VoiceScorerServicer, valid_pcm: bytes
    ) -> None:
        """Prevents the ONNX parity gate becoming unmeasurable through the RPC (frame_contract.md §6).

        ``raw_score`` is pre-calibration, so a recalibration must not move it. If only ``spoof_risk`` were
        returned, the parity gate would silently be measuring the calibration as well as the graph.
        """
        response = servicer.ScoreWindow(_request(valid_pcm), _FakeContext())
        assert response.raw_score != response.spoof_risk

    def test_ineligible_window_still_returns_a_score(
        self, servicer: VoiceScorerServicer, silent_window: np.ndarray
    ) -> None:
        """The Gateway decides what to do with it. The Scorer does not withhold data (rules.md R-10).

        Returning a score with ``eligible = false`` keeps the decision in one place. If the Scorer withheld
        the number, the Gateway could not record in the audit row what was actually observed.
        """
        response = servicer.ScoreWindow(_request(float32_to_pcm16(silent_window)), _FakeContext())
        assert response.eligible is False
        assert "LOW_ENERGY" in [pb.QualityFlag.Name(f) for f in response.quality_flags]

    @pytest.mark.contract
    @pytest.mark.parametrize("length", [0, 1, WINDOW_BYTES - 1, WINDOW_BYTES + 1, WINDOW_BYTES * 2])
    def test_wrong_window_size_aborts_invalid_argument(
        self, servicer: VoiceScorerServicer, length: int
    ) -> None:
        """Prevents a mis-assembled window being scored, and prevents it becoming an INTERNAL error.

        INVALID_ARGUMENT tells the Gateway this is the caller's problem and must not be retried;
        INTERNAL would make ``ScorerClient`` treat a permanent framing bug as a transient fault and retry
        it for the rest of the session.
        """
        with pytest.raises(_Aborted) as excinfo:
            servicer.ScoreWindow(_request(b"\x00" * length), _FakeContext())
        assert excinfo.value.code is grpc.StatusCode.INVALID_ARGUMENT

    @pytest.mark.contract
    def test_wrong_contract_id_aborts(
        self, servicer: VoiceScorerServicer, valid_pcm: bytes
    ) -> None:
        with pytest.raises(_Aborted) as excinfo:
            servicer.ScoreWindow(_request(valid_pcm, contract_id="raw-waveform-v2"), _FakeContext())
        assert excinfo.value.code is grpc.StatusCode.INVALID_ARGUMENT

    @pytest.mark.contract
    def test_wrong_sample_rate_aborts(
        self, servicer: VoiceScorerServicer, valid_pcm: bytes
    ) -> None:
        with pytest.raises(_Aborted) as excinfo:
            servicer.ScoreWindow(_request(valid_pcm, sample_rate_hz=8_000), _FakeContext())
        assert excinfo.value.code is grpc.StatusCode.INVALID_ARGUMENT

    @pytest.mark.privacy
    def test_abort_detail_never_echoes_the_request(self, servicer: VoiceScorerServicer) -> None:
        """rules.md R-17. Prevents caller-controlled text reaching CloudWatch through a status detail.

        The detail travels to the Gateway, into its structured log, and from there into CloudWatch. This
        sends a hostile ``contract_id`` and a payload with a recognisable marker, and asserts neither
        appears anywhere in the status.
        """
        marker = "MARKER-THAT-MUST-NOT-APPEAR-IN-ANY-STATUS"
        hostile_id = f"raw-waveform-v1 {marker}"
        payload = marker.encode() + b"\x00" * (WINDOW_BYTES - len(marker))
        with pytest.raises(_Aborted) as excinfo:
            servicer.ScoreWindow(_request(payload, contract_id=hostile_id), _FakeContext())
        assert marker not in excinfo.value.detail
        assert marker not in str(excinfo.value)

    @pytest.mark.privacy
    def test_servicer_retains_nothing_between_calls(
        self, servicer: VoiceScorerServicer, valid_pcm: bytes
    ) -> None:
        """rules.md R-14 and R-10 together: no audio kept, and no session state to keep it in.

        Asserted as an EMPTY instance namespace after repeated calls, not as an ``AttributeError`` on
        assignment. ``VoiceScorerServicer`` declares ``__slots__``, but its generated base class
        (``voice_scorer_pb2_grpc.VoiceScorerServicer``) does not, and one unslotted class in the MRO
        restores the instance ``__dict__``. Regenerated stubs are not ours to change, so the enforceable
        property is that nothing accumulates — which is the property that matters: a Scorer that grew
        session memory would also have grown its own evidence rule, outside the policy bundle.
        """
        for _ in range(5):
            servicer.ScoreWindow(_request(valid_pcm), _FakeContext())
        assert VoiceScorerServicer.__slots__ == ("_runtime",)
        assert (
            vars(servicer) == {}
        ), f"the servicer accumulated per-call state: {sorted(vars(servicer))}"

    @pytest.mark.privacy
    def test_no_module_on_the_serving_path_opens_a_file_for_writing(self) -> None:
        """rules.md R-14: no debug flag can write audio to disk, because no write path exists.

        Asserted on the source of the three modules a window passes through. ``model.py`` opens the ONNX
        artifact read-only to hash it; nothing anywhere opens a file for writing, serializes an array, or
        pickles anything. A future ``if DEBUG_DUMP_AUDIO:`` would have to introduce one of the constructs
        below, and this test is what would stop it at review time rather than at disclosure time.
        """
        write_constructs = (
            'open("w',
            "open('w",
            'open("a',
            "open('a",
            '"wb"',
            "'wb'",
            "write_bytes",
            "write_text",
            "np.save",
            ".tofile(",
            "pickle.dump",
            "shutil.copy",
        )
        for name in ("server.py", "contract.py", "model.py"):
            source = (Path(__file__).parents[1] / "app" / name).read_text(encoding="utf-8")
            for construct in write_constructs:
                assert construct not in source, f"{name} contains a write path: {construct}"


class TestHealth:
    """The parity set the Gateway refuses to start without (architecture.md §5.1)."""

    def test_health_reports_every_parity_field(self, servicer: VoiceScorerServicer) -> None:
        """Prevents a field being left at its proto default, which the Gateway would read as absent.

        ``gateway/app/main.py`` skips its model-hash comparison when ``model_sha256`` is falsy. An empty
        string there would turn a hard startup refusal into a silently skipped check.
        """
        response = servicer.Health(pb.HealthRequest(), _FakeContext())
        assert response.ready is True
        assert response.execution_provider == "CPUExecutionProvider"
        assert len(response.model_sha256) == 64
        assert len(response.calibration_sha256) == 64
        assert response.model_version
        assert response.calibration_version
        assert response.artifact_state == "research_only"

    def test_health_reports_the_detector_mode(self, servicer: VoiceScorerServicer) -> None:
        """rules.md R-46. The Gateway's banner renders this as a warning block."""
        response = servicer.Health(pb.HealthRequest(), _FakeContext())
        assert pb.DetectorMode.Name(response.detector_mode) == "MOCK_SMOKE_MODE_NOT_A_DETECTOR"

    def test_ready_is_true_because_unready_is_unreachable(
        self, servicer: VoiceScorerServicer
    ) -> None:
        """Not a stub. Every unfitness condition is checked before the port is bound.

        A provider fallback, a broken artifact pairing, and a failed contract-vector comparison all raise
        during ``build_runtime``. So there is no reachable state in which the servicer exists and the answer
        is "not ready" — and a soft ``false`` here would let a deploy gate wait hopefully on a process that
        is never going to become ready.
        """
        assert servicer.Health(pb.HealthRequest(), _FakeContext()).ready is True

    def test_unverified_parity_is_reported_as_false_not_true(
        self, servicer: VoiceScorerServicer
    ) -> None:
        """frame_contract.md §6. Prevents "not checked" being reported as "checked and passed".

        With no declared expected value there is nothing to compare against, and the honest answer is
        false. A default of true would mean the field claimed the check had run for the entire period
        before Pair B's Phase-3 parity gate produced a value — the window in which it matters most.
        """
        assert (
            servicer.Health(pb.HealthRequest(), _FakeContext()).contract_vector_parity_ok is False
        )

    def test_policy_eligibility_needs_both_the_artifact_and_the_state(
        self, runtime: ScorerRuntime
    ) -> None:
        """rules.md R-11 as one predicate. A placeholder can never produce True."""
        assert runtime.is_policy_eligible is False


class _FakeChannel:
    def __enter__(self) -> _FakeChannel:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _fake_health(monkeypatch: pytest.MonkeyPatch, **fields: Any) -> None:
    """Point :func:`healthcheck` at a canned ``HealthResponse`` instead of a socket."""
    defaults: dict[str, Any] = {
        "ready": True,
        "execution_provider": "CPUExecutionProvider",
        "model_version": "mock-smoke-not-a-detector",
        "model_sha256": "0" * 64,
        "calibration_version": "0.0.0-placeholder",
        "calibration_sha256": "0" * 64,
        "detector_mode": pb.DetectorMode.Value("MOCK_SMOKE_MODE_NOT_A_DETECTOR"),
        "artifact_state": "research_only",
        "contract_vector_parity_ok": False,
    }
    defaults.update(fields)
    response = pb.HealthResponse(**defaults)

    class _Stub:
        def __init__(self, channel: object) -> None:
            pass

        def Health(self, request: object, timeout: float | None = None) -> Any:
            return response

    monkeypatch.setattr(server_module.grpc, "insecure_channel", lambda target: _FakeChannel())
    monkeypatch.setattr(server_module.pb_grpc, "VoiceScorerStub", _Stub)


class TestHealthcheckVerdict:
    """The container HEALTHCHECK's exit code. In production, ECS reads nothing else.

    Every invariant enforced only inside ``build_runtime`` is invisible to the orchestrator: the process
    either started or it did not. These tests cover the invariants that must ALSO be visible at the
    boundary, and — just as importantly — the ones that must not fail here, because a healthcheck that
    cannot go green on the Phase-1 mock tier is a healthcheck that gets deleted.
    """

    def test_healthy_mock_tier_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``docker compose up`` is a Phase-1 exit criterion; mock mode must report healthy."""
        _fake_health(monkeypatch)
        assert server_module.healthcheck() == 0

    def test_not_ready_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_health(monkeypatch, ready=False)
        assert server_module.healthcheck() == 1

    def test_rpc_error_fails_without_echoing_the_exception(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A dead servicer must not read as healthy, and the status name is all that is printed."""

        class _Stub:
            def __init__(self, channel: object) -> None:
                pass

            def Health(self, request: object, timeout: float | None = None) -> Any:
                raise _FakeRpcError()

        monkeypatch.setattr(server_module.grpc, "insecure_channel", lambda target: _FakeChannel())
        monkeypatch.setattr(server_module.pb_grpc, "VoiceScorerStub", _Stub)
        assert server_module.healthcheck() == 1
        assert "UNAVAILABLE" in capsys.readouterr().err

    @pytest.mark.parity
    def test_provider_fallback_is_unhealthy(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """rules.md R-45. A GPU task serving from the CPU must not report healthy.

        ``OnnxDetector.assert_provider`` already raises on this at startup, so today the container never
        reaches a healthy state. This asserts the same invariant where the orchestrator can see it: if
        that startup assertion is ever softened to a warning — the single most tempting edit in
        ``model.py``, because a warning turns a failing deploy green — this is the check that still fails.
        """
        monkeypatch.setenv("EXECUTION_PROVIDER", "CUDAExecutionProvider")
        _fake_health(monkeypatch, execution_provider="CPUExecutionProvider")
        assert server_module.healthcheck() == 1
        stderr = capsys.readouterr().err
        assert "CUDAExecutionProvider" in stderr and "R-45" in stderr

    @pytest.mark.parity
    def test_matching_provider_is_healthy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EXECUTION_PROVIDER", "CUDAExecutionProvider")
        _fake_health(monkeypatch, execution_provider="CUDAExecutionProvider")
        assert server_module.healthcheck() == 0

    def test_absent_provider_variable_does_not_fail_the_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Prevents the CPU tier, which need not set the variable at all, reading as unhealthy."""
        monkeypatch.delenv("EXECUTION_PROVIDER", raising=False)
        _fake_health(monkeypatch, execution_provider="CPUExecutionProvider")
        assert server_module.healthcheck() == 0

    def test_malformed_provider_variable_reports_no_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Prevents a typo in the task definition being reported as a broken model.

        ``load_settings`` already refuses an unrecognized value at startup, so a process answering Health
        cannot be running one. Reaching here with ``"cuda"`` therefore means this environment differs from
        the server's — a wiring bug. "Unhealthy" is the wrong verdict for it: it would page someone to
        look at the detector while the fault is in the task definition.
        """
        monkeypatch.setenv("EXECUTION_PROVIDER", "cuda")
        _fake_health(monkeypatch, execution_provider="CPUExecutionProvider")
        assert server_module.healthcheck() == 0

    def test_unverified_parity_alone_is_still_healthy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``contract_vector_parity_ok == False`` means UNVERIFIED, and a FAILURE never gets this far.

        A declared-and-mismatched expected score raises ``ModelLoadError`` in ``_check_contract_vector``
        before the port is bound. False is what the placeholder calibration necessarily produces, because
        it declares no expected score at all (rules.md R-11). Failing on it would make every Phase-1
        container permanently unhealthy.
        """
        _fake_health(monkeypatch, contract_vector_parity_ok=False, artifact_state="research_only")
        assert server_module.healthcheck() == 0

    @pytest.mark.parity
    def test_policy_eligible_with_unverified_parity_is_unhealthy(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The one combination that would let an off-contract build be called policy-eligible.

        Unreachable through ``_supported_artifact_state``, which caps the state at ``demo_eligible``
        whenever parity is unverified. Asserted anyway, because the cost of a tripwire on a structural
        invariant is four lines and the cost of the invariant quietly ceasing to hold is a release
        manifest that claims a calibrated probability from a build whose preprocessing was never checked.
        """
        _fake_health(monkeypatch, artifact_state="policy_eligible", contract_vector_parity_ok=False)
        assert server_module.healthcheck() == 1
        assert "policy_eligible" in capsys.readouterr().err

    @pytest.mark.parity
    def test_policy_eligible_with_verified_parity_is_healthy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_health(monkeypatch, artifact_state="policy_eligible", contract_vector_parity_ok=True)
        assert server_module.healthcheck() == 0


class TestStartupSequence:
    """technical-design.md §7, in order, failing fast at each step."""

    def test_model_pairing_mismatch_refuses(self, fitted_calibration: Calibration) -> None:
        """Step 1. Prevents thresholds fitted on one model being applied to another.

        A mis-calibration with no symptom: the service is healthy, the scores are in range, and the
        threshold sits somewhere nobody chose.
        """

        class _OtherModel:
            model_sha256 = "f" * 64

        with pytest.raises(ModelLoadError, match="model_sha256"):
            _verify_model_pairing(fitted_calibration, _OtherModel(), is_mock=False)  # type: ignore[arg-type]

    def test_model_pairing_matches(self, fitted_calibration: Calibration) -> None:
        class _MatchingModel:
            model_sha256 = EXAMPLE_MODEL_SHA256

        _verify_model_pairing(fitted_calibration, _MatchingModel(), is_mock=False)  # type: ignore[arg-type]

    def test_mock_mode_skips_the_pairing_check(self, fitted_calibration: Calibration) -> None:
        """There is no model, so there is nothing to pair; comparing two placeholders proves nothing."""

        class _NoModel:
            model_sha256 = "0" * 64

        _verify_model_pairing(fitted_calibration, _NoModel(), is_mock=True)  # type: ignore[arg-type]

    def test_contract_vector_mismatch_refuses_to_start(
        self,
        mock_settings: ScorerSettings,
        fitted_calibration_document: dict[str, object],
        write_calibration: Callable[..., Path],
    ) -> None:
        """Step 3. Prevents a mismatched artifact pairing reaching a demo (frame_contract.md §6).

        The declared expected score is deliberately wrong here. A mismatch is fatal, not a warning: the
        loaded model does not reproduce the score recorded with this calibration, so one of the two
        artifacts is not the one the release manifest describes.
        """
        document = dict(fitted_calibration_document)
        document["contract_vector_raw_score"] = 999.0
        calibration = load_calibration(write_calibration(document))
        detector = build_detector(mock_settings, model_version="unused")
        with pytest.raises(ModelLoadError, match="parity FAILED"):
            _check_contract_vector(mock_settings, detector, calibration)

    def test_contract_vector_match_reports_verified(
        self,
        mock_settings: ScorerSettings,
        fitted_calibration_document: dict[str, object],
        write_calibration: Callable[..., Path],
        contract_vector: np.ndarray,
    ) -> None:
        """Uses the mock's own deterministic score as the expected value.

        That is exactly why the mock is deterministic: the parity mechanism can be tested end to end
        before any real model exists, so the mechanism is known to work on the day the model lands.
        """
        detector = build_detector(mock_settings, model_version="unused")
        expected = detector.score(contract_vector, session_ref="contract-vector-v1")
        document = dict(fitted_calibration_document)
        document["contract_vector_raw_score"] = expected
        calibration = load_calibration(write_calibration(document))
        assert _check_contract_vector(mock_settings, detector, calibration) is True

    def test_absent_expectation_reports_unverified(
        self, mock_settings: ScorerSettings, fitted_calibration: Calibration
    ) -> None:
        detector = build_detector(mock_settings, model_version="unused")
        assert _check_contract_vector(mock_settings, detector, fitted_calibration) is False

    def test_missing_fixture_with_a_declared_expectation_refuses(
        self,
        base_env: dict[str, str],
        fitted_calibration_document: dict[str, object],
        write_calibration: Callable[..., Path],
        tmp_path: Path,
    ) -> None:
        """Prevents the parity check being skipped by forgetting a mount when it was meant to run."""
        env = dict(base_env)
        env["CONTRACT_VECTOR_PATH"] = str(tmp_path / "absent.npy")
        settings = load_settings(env=env)
        document = dict(fitted_calibration_document)
        document["contract_vector_raw_score"] = 0.5
        calibration = load_calibration(write_calibration(document))
        detector = build_detector(settings, model_version="unused")
        with pytest.raises(ModelLoadError):
            _check_contract_vector(settings, detector, calibration)

    def test_supported_artifact_state_is_derived_from_the_artifacts(
        self, fitted_calibration: Calibration
    ) -> None:
        """Derived, never declared. A process cannot assert its own eligibility."""
        assert (
            _supported_artifact_state(
                is_mock=True, calibration=fitted_calibration, contract_vector_parity_ok=True
            )
            is ArtifactState.RESEARCH_ONLY
        )
        assert (
            _supported_artifact_state(
                is_mock=False,
                calibration=placeholder_calibration(),
                contract_vector_parity_ok=True,
            )
            is ArtifactState.DEMO_ELIGIBLE
        )
        assert (
            _supported_artifact_state(
                is_mock=False, calibration=fitted_calibration, contract_vector_parity_ok=False
            )
            is ArtifactState.DEMO_ELIGIBLE
        )
        assert (
            _supported_artifact_state(
                is_mock=False, calibration=fitted_calibration, contract_vector_parity_ok=True
            )
            is ArtifactState.POLICY_ELIGIBLE
        )

    def test_mock_mode_falls_back_to_the_placeholder_calibration(
        self, mock_settings: ScorerSettings
    ) -> None:
        """Prevents the Phase-1 Compose tier being blocked on a Phase-2 artifact.

        ``base_env`` points ``CALIBRATION_PATH`` at a file that does not exist, which is the real Day-1
        situation: ``policy/`` is empty until Pair B's Phase-2 deliverable lands.
        """
        runtime = build_runtime(mock_settings)
        assert runtime.calibration.version == "0.0.0-placeholder"
        assert runtime.is_policy_eligible is False
        assert runtime.artifact_state is ArtifactState.RESEARCH_ONLY

    def test_real_mode_with_a_missing_calibration_refuses(self, base_env: dict[str, str]) -> None:
        """Prevents a real model serving against a mapping nobody fitted.

        The asymmetry with mock mode is deliberate: the placeholder exists so the transport can be tested
        before the ML artifacts land, not so a real detector can serve uncalibrated numbers.
        """
        env = dict(base_env)
        env["DETECTOR_MODE"] = "REAL_DETECTOR"
        settings = load_settings(env=env)
        with pytest.raises(CalibrationError) as excinfo:
            build_runtime(settings)
        assert "calibration" in str(excinfo.value).lower()

    def test_malformed_calibration_is_fatal_even_in_mock_mode(
        self, base_env: dict[str, str], tmp_path: Path
    ) -> None:
        """ "The file is there and wrong" is not the same situation as "it does not exist yet".

        Silently ignoring a broken artifact would hide it from the person who just wrote it, and the demo
        would then run on the placeholder while the manifest named the real file.
        """
        broken = tmp_path / "calibration.json"
        broken.write_text('{"status": "fitted-dev-calibration"}', encoding="utf-8")
        env = dict(base_env)
        env["CALIBRATION_PATH"] = str(broken)
        # The specific type matters. ``pytest.raises(Exception)`` here would also pass if build_runtime
        # died of an AttributeError or a TypeError while parsing, which would mean the loader crashed
        # rather than refused — a crash and a refusal look the same to a test that only asks "did it
        # raise", and only one of them is the designed behaviour.
        with pytest.raises(CalibrationError):
            build_runtime(load_settings(env=env))


class TestServerConstruction:
    """Bounded pool, bounded in-flight RPCs, and a port that is bound only after startup succeeds."""

    def test_server_is_created_with_bounded_concurrency(self, runtime: ScorerRuntime) -> None:
        """Prevents unbounded queueing, which is invisible backpressure and retained audio.

        Without ``maximum_concurrent_rpcs`` gRPC accepts requests without limit and parks them in the
        pool's work queue, each holding an 81,920-byte window. The Gateway's own semaphore can only see
        what it sent, so the saturation would be invisible on the side that is supposed to shed load
        (rules.md R-20).
        """
        source = (Path(__file__).parents[1] / "app" / "server.py").read_text(encoding="utf-8")
        assert "maximum_concurrent_rpcs=settings.grpc_max_concurrent_rpcs" in source
        assert "max_workers=settings.grpc_max_workers" in source

    def test_server_binds_and_stops_cleanly(
        self, base_env: dict[str, str], mock_settings: ScorerSettings
    ) -> None:
        """Smoke test for the real grpc.server options list, which a typo would break at startup only.

        An OS-assigned ephemeral port is discovered first and then configured, rather than passing
        ``GRPC_PORT=0``. ``config.py`` refuses port 0 on purpose — a service that bound an arbitrary port
        would be unreachable at the address the Gateway and the ECS service-discovery record both name,
        and it would come up "healthy" while doing it. Weakening that check so a test could use port 0
        would remove a real guard to make a test convenient.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        env = dict(base_env)
        env["GRPC_PORT"] = str(port)
        server = create_server(
            ScorerRuntime(
                settings=load_settings(env=env),
                detector=build_detector(mock_settings, model_version="unused"),
                calibration=placeholder_calibration(),
                artifact_state=ArtifactState.RESEARCH_ONLY,
                contract_vector_parity_ok=False,
                proto_sha256="0" * 64,
            )
        )
        server.start()
        server.stop(grace=None).wait(timeout=5)

    def test_transport_is_insecure_by_design_and_documented(self) -> None:
        """rules.md R-01. Prevents mTLS being described as present when it is a Phase-4 target.

        gRPC never crosses the edge: inside the VPC with the Gateway's security group as the only ingress,
        and over the Compose network locally with no published host port. That is a real control, and it is
        not mTLS — writing it down here is what stops the demo script from claiming otherwise.
        """
        source = (Path(__file__).parents[1] / "app" / "server.py").read_text(encoding="utf-8")
        assert "add_insecure_port" in source
        assert "mTLS" in source and "Phase-4" in source
