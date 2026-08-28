"""gRPC server for ``VoiceScorer``. Startup is a fail-fast sequence; serving is stateless per window.

STARTUP ORDER IS NORMATIVE (technical-design.md §7). Each step exists to make a specific silent
failure impossible, and each one runs BEFORE the port is bound, so an unhealthy artifact pairing can
never be reached by a request:

1. Load ``calibration.json``. Verify its ``model_sha256`` equals the SHA-256 of the ONNX file actually
   loaded. Thresholds fitted against one model's score distribution and applied to another's is a
   mis-calibration with no symptom.
2. Create the ORT session and assert the requested execution provider is the one in use. A silent CPU
   fallback on the GPU tier is a FAILURE, not a degradation (rules.md R-45) — see ``model.py`` for why
   it is worse than a crash.
3. Score the fixed 40,960-sample contract vector and compare against the artifact's declared expected
   value within ``CONTRACT_VECTOR_ATOL`` (frame_contract.md §6).
4. Print the banner. Serve.

WHAT THIS SERVICE CANNOT DO, STRUCTURALLY
It receives no ``purpose_code``, no session history, and no action field, and it has nowhere to return
one. The detection/decision seam is enforced by the message shape in ``contracts/voice_scorer.proto``,
not by a convention in this file — ``tests/test_detection_decision_seam.py`` inspects the generated
descriptor and fails if a policy-shaped field ever appears. The Scorer produces a number; the Gateway
decides (rules.md R-10).

NO AUDIO PERSISTS (rules.md R-14). The window exists as a ``bytes`` field on a request message and as
one float32 array for the duration of one call. Nothing in this module opens a file for writing, and
there is no debug flag that changes that: the log sink in ``banner.py`` refuses to render ``bytes`` at
all, so even a ``logger.debug(pcm)`` added later emits a length, not a payload.

A THREAD POOL, NOT ASYNCIO. ``InferenceSession.run`` is a blocking call into C++ that releases the GIL,
so threads give real parallelism where an event loop would serialize every inference behind one core.
The pool is bounded and ``maximum_concurrent_rpcs`` is set: the Scorer refuses rather than queues,
because a queue inside this process is invisible to the Gateway's backpressure decision, and queued
audio is retained audio (rules.md R-20).
"""

from __future__ import annotations

import os
import signal
import sys
import time
from concurrent import futures
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Final

import grpc
import numpy as np

from app import voice_scorer_pb2 as pb
from app import voice_scorer_pb2_grpc as pb_grpc
from app.banner import build_banner, configure_logging, emit_banner, get_logger
from app.calibration import (
    Calibration,
    CalibrationError,
    load_calibration,
    placeholder_calibration,
)
from app.config import (
    DEFAULT_GRPC_PORT,
    ArtifactState,
    ConfigError,
    ExecutionProvider,
    ScorerSettings,
    load_settings,
)
from app.contract import (
    CLIPPING_MAGNITUDE,
    CLIPPING_SAMPLE_FRACTION,
    DC_OFFSET_MEAN_ABS,
    LOW_ENERGY_RMS_FLOOR,
    ONNX_INPUT_SHAPE,
    ContractViolation,
    pcm16_to_float32,
    validate_window_request,
)
from app.model import Detector, ModelLoadError, ProviderUnavailable, build_detector

_log = get_logger(__name__)

#: Read at startup only, to hash for the parity set. Copied into the image by ``scorer/Dockerfile``
#: from the repo-root build context; absent in a source checkout run from a different directory, in
#: which case the hash reports "unavailable" rather than refusing to serve — the authoritative copy is
#: the release manifest, and a missing file here is a packaging detail.
_PROTO_PATH: Final[Path] = Path("contracts/voice_scorer.proto")

#: Flags that make a window INELIGIBLE for the k-of-n evidence count.
#:
#: Deliberately short. An ineligible window is SKIPPED, never counted as low-risk (rules.md R-09), so
#: every flag added here is a channel an attacker can use to suppress a decision: degrade the audio in
#: the flagged way and no evidence accumulates. ``LOW_ENERGY`` is on the list because scoring silence
#: is forbidden outright (playbook §1: do not infer from silence) and because an attacker who goes
#: quiet supplies no synthetic speech to detect — there is nothing to suppress. ``CLIPPING_DETECTED``
#: and ``DC_OFFSET`` are reported but NOT disqualifying: a caller who clips their own audio would
#: otherwise switch the detector off, which is a far cheaper attack than defeating it.
DISQUALIFYING_FLAGS: Final[frozenset[str]] = frozenset({"LOW_ENERGY", "INSUFFICIENT_VOICED"})


# -- quality ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QualityAssessment:
    """Observations about the WINDOW. Never about the speaker (rules.md R-41)."""

    flags: tuple[str, ...]
    eligible: bool


def assess_window(window: np.ndarray) -> QualityAssessment:
    """Describe the window's signal quality. Pure: no I/O, no clock, no randomness.

    Every flag here is a property of the channel or the capture, and none is spoof evidence. Nothing
    in this function may key on accent, emotion, illness, gender, age, or speaking style (rules.md
    R-41), and nothing may treat a sampling-rate or bandwidth observation as a decision rule (rules.md
    R-39) — which is why ``NARROWBAND_SUSPECTED`` is NOT computed here. Estimating bandwidth needs a
    spectral transform, and a spectral boundary in the serving path is exactly the hard-coded frequency
    cutoff rules.md R-12 forbids; that observation belongs to the ablation-gated diagnostics plane, and
    it stays out of this file until that gate passes.

    The flags' only effect is on ``eligible``. They never move ``spoof_risk`` in either direction.
    """
    if window.shape != ONNX_INPUT_SHAPE:
        raise ValueError("window must have shape (1, 40960)")

    samples = window.reshape(-1)
    flags: list[str] = []

    # float64 accumulation: 40,960 squared float32 values summed in float32 loses precision near the
    # low-energy floor, which is the one place this comparison has to be right.
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    if rms < LOW_ENERGY_RMS_FLOOR:
        flags.append("LOW_ENERGY")

    clipped = float(np.mean(np.abs(samples, dtype=np.float64) >= CLIPPING_MAGNITUDE))
    if clipped > CLIPPING_SAMPLE_FRACTION:
        flags.append("CLIPPING_DETECTED")

    if abs(float(np.mean(samples, dtype=np.float64))) > DC_OFFSET_MEAN_ABS:
        flags.append("DC_OFFSET")

    eligible = not any(flag in DISQUALIFYING_FLAGS for flag in flags)
    return QualityAssessment(flags=tuple(flags), eligible=eligible)


# -- runtime ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScorerRuntime:
    """Everything the servicer needs, assembled and validated before the port is bound.

    Frozen because none of it may change while the process serves. A model or calibration swapped at
    runtime would mean two audit rows in one session carrying different artifact hashes, and the parity
    set would describe neither.
    """

    settings: ScorerSettings
    detector: Detector
    calibration: Calibration
    artifact_state: ArtifactState
    contract_vector_parity_ok: bool
    proto_sha256: str

    @property
    def is_policy_eligible(self) -> bool:
        """Whether this process may present its output as a policy-eligible measurement.

        Requires the calibration artifact to say so AND the derived artifact state to agree. A
        placeholder calibration therefore LOADS and serves, but can never produce a ``true`` here —
        which is the whole of rules.md R-11 expressed as one predicate.
        """
        return (
            self.calibration.is_policy_eligible
            and self.artifact_state is ArtifactState.POLICY_ELIGIBLE
        )


def _hash_proto() -> str:
    candidates = [
        _PROTO_PATH,
        Path("..") / _PROTO_PATH,
        Path(__file__).resolve().parent.parent.parent / _PROTO_PATH,
        Path("/") / _PROTO_PATH,
    ]
    for c in candidates:
        try:
            if c.is_file():
                return sha256(c.read_bytes()).hexdigest()
        except OSError:
            continue
    return "unavailable"


def _load_calibration_for(settings: ScorerSettings) -> Calibration:
    """Step 1 of the startup sequence, with one documented exception for mock mode.

    In real mode a missing or invalid artifact is a hard refusal: a model whose output nobody has
    calibrated has no defensible mapping onto a risk, and inventing one would put a number in front of
    a judge that no one fitted.

    In mock mode a missing file falls back to :func:`placeholder_calibration`. ``policy/`` is Pair B's
    Phase-2 deliverable and is empty in Phase 1, while ``docker compose up`` and the Gateway's WSS
    contract suite are Phase-1 exit criteria (phases.md §2.1, §2.4). Refusing to start would block two
    pairs on an artifact two days out. A malformed file is still fatal even in mock mode — "the file is
    there and wrong" is a different situation from "the file does not exist yet", and silently ignoring
    the first would hide a broken artifact from the person who just wrote it.
    """
    if settings.is_mock and not settings.calibration_path.exists():
        _log.warning(
            "no calibration artifact; using the built-in placeholder (mock mode only)",
            extra={"component": "startup", "calibration_version": "0.0.0-placeholder"},
        )
        return placeholder_calibration()
    return load_calibration(settings.calibration_path)


def _verify_model_pairing(calibration: Calibration, detector: Detector, *, is_mock: bool) -> None:
    """Step 1's second half: the calibration must name the model that is actually loaded."""
    if is_mock:
        # There is no model, so there is nothing to pair. The mock's model_sha256 is the hash of the
        # empty string and its model_version says so; comparing them would only assert that two
        # placeholders match each other.
        return
    if calibration.model_sha256 != detector.model_sha256:
        raise ModelLoadError(
            "calibration artifact model_sha256 does not match the loaded ONNX file. The thresholds "
            "were fitted against a different model's score distribution; applying them to this one is "
            "a mis-calibration with no visible symptom (technical-design.md §7 step 1)."
        )


def _check_contract_vector(
    settings: ScorerSettings, detector: Detector, calibration: Calibration
) -> bool:
    """Step 3: re-score the fixed vector. Returns whether parity was VERIFIED, not whether it passed.

    Three outcomes, and the distinction between the last two is the point (frame_contract.md §6):

    * The artifact declares an expected raw score and the re-scored value is outside ``atol`` ⇒ raise.
      This is a mismatched artifact pairing and it must not reach a demo.
    * The artifact declares an expected value and it matches ⇒ ``True``.
    * The artifact declares nothing, or the fixture is absent ⇒ ``False``, reported as
      ``contract_vector_parity_ok = false`` and surfaced as a warning block in the banner. UNVERIFIED
      is reported as unverified rather than as passing; a default of ``True`` here would mean the field
      said "checked" for the entire period before Pair B's Phase-3 parity gate produced a value to
      check against, which is the window in which it matters most.
    """
    if calibration.contract_vector_raw_score is None:
        _log.warning(
            "calibration artifact declares no expected contract-vector score; parity is UNVERIFIED",
            extra={"component": "startup", "calibration_version": calibration.version},
        )
        return False

    try:
        # allow_pickle=False: a .npy file can carry a pickled object, and unpickling is arbitrary code
        # execution. This fixture is a plain float32 array and must load as one.
        vector = np.load(settings.contract_vector_path, allow_pickle=False)
    except (OSError, ValueError) as exc:
        raise ModelLoadError(
            f"contract test vector could not be loaded: {settings.contract_vector_path}. The "
            "calibration artifact declares an expected score for it, so it cannot be skipped "
            "(frame_contract.md §6)."
        ) from exc

    vector = np.ascontiguousarray(vector, dtype=np.float32).reshape(ONNX_INPUT_SHAPE)
    actual = detector.score(vector, session_ref="contract-vector-v1")
    delta = abs(actual - calibration.contract_vector_raw_score)

    if delta > settings.contract_vector_atol:
        raise ModelLoadError(
            "contract test vector parity FAILED. The loaded model does not reproduce the raw score "
            "recorded with this calibration artifact, so the artifact pairing is wrong. This blocks "
            "deployment (frame_contract.md §6, phases.md §4.1)."
        )

    _log.info(
        "contract vector parity ok",
        extra={"component": "startup", "raw_score": actual},
    )
    return True


def _supported_artifact_state(
    *, is_mock: bool, calibration: Calibration, contract_vector_parity_ok: bool
) -> ArtifactState:
    """The strongest state the LOADED ARTIFACTS support, ignoring what the manifest claims.

    Mirrors ``gateway/app/policy/loader.py::PolicyBundle.artifact_state``: derived, never declared. A
    process cannot assert its own eligibility — the artifacts have to earn it.
    """
    if is_mock:
        return ArtifactState.RESEARCH_ONLY
    if not calibration.is_policy_eligible or not contract_vector_parity_ok:
        return ArtifactState.DEMO_ELIGIBLE
    return ArtifactState.POLICY_ELIGIBLE


def build_runtime(settings: ScorerSettings) -> ScorerRuntime:
    """The startup sequence from technical-design.md §7, in order, failing fast at each step."""
    calibration = _load_calibration_for(settings)

    detector = build_detector(settings, model_version=calibration.model_version)
    _verify_model_pairing(calibration, detector, is_mock=settings.is_mock)

    contract_vector_parity_ok = _check_contract_vector(settings, detector, calibration)

    artifact_state = settings.capped_artifact_state(
        supported=_supported_artifact_state(
            is_mock=settings.is_mock,
            calibration=calibration,
            contract_vector_parity_ok=contract_vector_parity_ok,
        )
    )

    return ScorerRuntime(
        settings=settings,
        detector=detector,
        calibration=calibration,
        artifact_state=artifact_state,
        contract_vector_parity_ok=contract_vector_parity_ok,
        proto_sha256=_hash_proto(),
    )


# -- servicer --------------------------------------------------------------------------------------


class VoiceScorerServicer(pb_grpc.VoiceScorerServicer):
    """Stateless per window. Holds no session state, no counters, and no reference to any payload.

    ``window_seq`` and ``session_ref`` are read for the log line and for the mock's determinism, and
    for nothing else. The proto states the Scorer must not use ``window_seq`` to correlate windows, and
    there is no member on this class in which a correlation could be stored.
    """

    __slots__ = ("_runtime",)

    def __init__(self, runtime: ScorerRuntime):
        self._runtime = runtime

    # -- ScoreWindow -------------------------------------------------------------------------------

    # PascalCase method names are fixed by the generated base class and are not ours to rename.
    def ScoreWindow(self, request: Any, context: grpc.ServicerContext) -> Any:
        started_ns = time.perf_counter_ns()
        runtime = self._runtime

        try:
            validate_window_request(
                pcm_window=request.pcm_window,
                contract_id=request.contract_id,
                sample_rate_hz=request.sample_rate_hz,
            )
            window = pcm16_to_float32(request.pcm_window)
        except ContractViolation as violation:
            # The status detail is the STATIC text from contract.py. The sizes go to the log line via
            # allow-listed numeric keys, never into the message (rules.md R-17): a detail string built
            # from the request would let a payload fragment or a caller reference reach the Gateway's
            # logger and from there CloudWatch.
            _log.warning(
                "rejected window",
                extra={
                    "component": "score",
                    "code": violation.code,
                    "window_seq": request.window_seq,
                    "bytes_expected": violation.expected,
                    "bytes_actual": violation.actual,
                },
            )
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, violation.detail)
            # ``from None`` deliberately: this line is unreachable because ``abort`` always raises, so if
            # it ever DOES run, the fault is a change in grpc's abort contract — not the rejected window.
            # Chaining the ContractViolation as the cause would point whoever reads the traceback at the
            # caller's malformed request instead of at the library behaviour that actually broke.
            # An explicit ``raise AssertionError`` rather than ``assert False``: ``python -O`` strips the
            # latter, which would turn this guard into a silent ``return None`` on the serving path.
            raise AssertionError("unreachable: context.abort raises") from None  # pragma: no cover

        quality = assess_window(window)
        raw_score = runtime.detector.score(window, session_ref=request.session_ref)
        spoof_risk = runtime.calibration.apply(raw_score)

        latency_us = (time.perf_counter_ns() - started_ns) // 1_000

        _log.info(
            "scored window",
            extra={
                "component": "score",
                "call_ref": request.session_ref,
                "window_seq": request.window_seq,
                "spoof_risk": round(spoof_risk, 4),
                "raw_score": round(raw_score, 6),
                "eligible": quality.eligible,
                "quality_flags": list(quality.flags),
                "scorer_latency_us": latency_us,
                # On the score line specifically as well as on every line: a log search that lands on
                # one latency figure must carry the label with it (rules.md R-46).
                "detector_mode": runtime.settings.detector_mode.value,
            },
        )

        # `window` and `request.pcm_window` both fall out of scope when this frame returns. No branch
        # above writes either to disk, and there is no configuration that adds one (rules.md R-14).
        return pb.ScoreWindowResponse(
            spoof_risk=spoof_risk,
            model_version=runtime.detector.model_version,
            calibration_version=runtime.calibration.version,
            quality_flags=[pb.QualityFlag.Value(flag) for flag in quality.flags],
            eligible=quality.eligible,
            raw_score=raw_score,
            scorer_latency_us=latency_us,
            detector_mode=pb.DetectorMode.Value(runtime.settings.detector_mode.value),
        )

    # -- Health ------------------------------------------------------------------------------------

    # PascalCase, as above: the generated base class defines the name.
    def Health(self, request: Any, context: grpc.ServicerContext) -> Any:
        """Liveness plus artifact identity — half of the parity set (architecture.md §5.1).

        ``ready`` is unconditionally ``True`` here, and that is not a stub. Every condition that could
        make this process unfit to serve is checked in :func:`build_runtime`, before the port is bound:
        a provider fallback, a broken artifact pairing, or a failed contract-vector comparison all raise
        and the process exits. So there is no reachable state in which the servicer exists and the
        answer is "not ready" — reporting a soft ``false`` instead would let a deploy gate wait
        hopefully on a process that is never going to become ready.

        ``contract_vector_parity_ok`` CAN be ``false`` here. It means the comparison was not performed
        (no declared expected value, per :func:`_check_contract_vector`) — never that it was performed
        and failed, because that case raised at startup.
        """
        runtime = self._runtime
        return pb.HealthResponse(
            ready=True,
            execution_provider=runtime.detector.execution_provider,
            model_version=runtime.detector.model_version,
            model_sha256=runtime.detector.model_sha256,
            calibration_version=runtime.calibration.version,
            calibration_sha256=runtime.calibration.sha256,
            detector_mode=pb.DetectorMode.Value(runtime.settings.detector_mode.value),
            artifact_state=runtime.artifact_state.value,
            contract_vector_parity_ok=runtime.contract_vector_parity_ok,
        )


# -- serving ---------------------------------------------------------------------------------------


def create_server(runtime: ScorerRuntime) -> grpc.Server:
    """Build the gRPC server. Bounded pool, bounded in-flight RPCs, insecure by design.

    Insecure because gRPC never crosses the edge: inside the VPC on AWS with the Gateway security
    group as the only ingress, over the Compose network locally with no published host port. mTLS
    between Gateway and Scorer is a Phase-4 production-hardening target and must not be described as
    present today (rules.md R-01, and the transport-scope note in ``contracts/voice_scorer.proto``).
    """
    settings = runtime.settings
    server = grpc.server(
        futures.ThreadPoolExecutor(
            max_workers=settings.grpc_max_workers, thread_name_prefix="score"
        ),
        # Refuse rather than queue. Without this, gRPC accepts unbounded requests and parks them in
        # the pool's work queue — each holding an 81,920-byte window in memory where the Gateway
        # cannot see the backpressure (rules.md R-20).
        maximum_concurrent_rpcs=settings.grpc_max_concurrent_rpcs,
        options=[
            ("grpc.keepalive_time_ms", 20_000),
            ("grpc.keepalive_timeout_ms", 5_000),
            ("grpc.keepalive_permit_without_calls", 1),
            ("grpc.http2.min_ping_interval_without_data_ms", 5_000),
            ("grpc.http2.max_ping_strikes", 0),
            # One window in, one small message out. A cap just above the real payload turns a
            # malformed or hostile length prefix into a rejected frame instead of a large allocation.
            ("grpc.max_receive_message_length", 1024 * 1024),
            ("grpc.max_send_message_length", 64 * 1024),
        ],
    )
    pb_grpc.add_VoiceScorerServicer_to_server(VoiceScorerServicer(runtime), server)
    server.add_insecure_port(f"0.0.0.0:{settings.grpc_port}")
    return server


def serve() -> int:
    """Load config, run the startup sequence, print the banner, serve until SIGTERM."""
    try:
        settings = load_settings()
    except ConfigError as exc:
        # Before logging is configured, so this one goes to stderr unformatted. A configuration error
        # that could not be read because the logger it needed was not up yet is a bad first minute.
        print(f"scorer: refusing to start: {exc}", file=sys.stderr)
        return 78  # EX_CONFIG

    configure_logging(settings)

    try:
        runtime = build_runtime(settings)
    except (CalibrationError, ModelLoadError, ProviderUnavailable) as exc:
        _log.error(
            "startup failed; refusing to serve", extra={"component": "startup"}, exc_info=exc
        )
        return 1

    emit_banner(
        build_banner(
            settings,
            calibration=runtime.calibration,
            model_version=runtime.detector.model_version,
            model_sha256=runtime.detector.model_sha256,
            execution_provider=runtime.detector.execution_provider,
            artifact_state=runtime.artifact_state,
            contract_vector_parity_ok=runtime.contract_vector_parity_ok,
            proto_sha256=runtime.proto_sha256,
        ),
        _log,
    )

    server = create_server(runtime)
    server.start()

    def _shutdown(signum: int, _frame: Any) -> None:
        # ECS sends SIGTERM and waits out StopTimeout before SIGKILL. Draining in-flight scores rather
        # than dropping them means a rolling deploy does not manufacture SCORER_UNAVAILABLE close codes
        # in the Gateway for windows that were already being scored.
        _log.info(
            "shutting down", extra={"component": "lifecycle", "code": signal.Signals(signum).name}
        )
        server.stop(grace=5.0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    server.wait_for_termination()
    runtime.detector.close()
    return 0


def healthcheck() -> int:
    """Exercise the real ``Health`` RPC over the loopback. Used by the container HEALTHCHECK.

    A TCP port check would pass while the model was unloaded, the provider had fallen back, or the
    servicer was raising on every call — "the port is open" is not "the service works", and on the tier
    where that distinction matters the difference is a demo that starts and produces nothing.

    Deliberately does NOT call :func:`load_settings`. A healthcheck that fails because an unrelated
    environment variable is malformed reports the wrong problem, and it would report it as "unhealthy"
    with no way to tell the two apart. Only ``GRPC_PORT`` and ``EXECUTION_PROVIDER`` are read, both
    defensively.

    WHY THE PROVIDER IS CHECKED HERE AS WELL AS AT STARTUP
    ``OnnxDetector.assert_provider`` already refuses to finish loading on a fallback, so a real-mode task
    that resolved to CPU on the GPU tier crashes rather than serving. This is the same invariant asserted
    at the boundary that production actually reads: ECS decides whether a task is healthy from the
    container healthcheck and from nothing else, so an invariant enforced only on a startup path is one
    the orchestrator cannot see. If a later change ever downgrades that assertion to a warning — the
    single most tempting edit in this file, because a warning makes a failing deploy go green — this
    check is what still fails. A silent CUDA→CPU fallback is worse than a crash: the task reports
    healthy, produces plausible numbers at a fraction of the claimed throughput, and writes
    ``execution_provider`` into every audit row, so the evidence and the latency claims disagree with
    nothing to flag it (rules.md R-45).

    WHY ``contract_vector_parity_ok == False`` IS NOT BY ITSELF UNHEALTHY
    That field means UNVERIFIED, not FAILED. A parity FAILURE — the artifact declares an expected raw
    score for ``contract_vector_v1.npy`` and the loaded model does not reproduce it — is already fatal in
    :func:`_check_contract_vector`, which raises ``ModelLoadError`` before the port is ever bound. False
    is what the placeholder calibration necessarily produces, because it declares no expected score to
    compare against (rules.md R-11). Failing the healthcheck on it would make the entire Phase-1 mock
    tier permanently unhealthy and ``docker compose up`` — a Phase-1 exit criterion — could never go
    green, which would get the healthcheck deleted rather than the artifact fixed. What IS checked is the
    pairing: a service claiming ``policy_eligible`` with unverified parity is a contradiction.
    """
    port = os.environ.get("GRPC_PORT", str(DEFAULT_GRPC_PORT))
    try:
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = pb_grpc.VoiceScorerStub(channel)
            response = stub.Health(pb.HealthRequest(), timeout=3.0)
    except grpc.RpcError as exc:
        print(f"scorer healthcheck: {exc.code().name}", file=sys.stderr)  # type: ignore[union-attr]
        return 1
    if not response.ready:
        print("scorer healthcheck: not ready", file=sys.stderr)
        return 1

    # Compared through the enum, not as a raw string. An unrecognized value means the deployment is
    # misconfigured, and `load_settings` has already made that fatal at startup — so reaching here with
    # one means this process's environment differs from the server's, which is a wiring bug and not
    # evidence that the service is unhealthy. Reporting "unhealthy" for it would send whoever is paged
    # looking at the model instead of at the task definition.
    declared_provider = os.environ.get("EXECUTION_PROVIDER", "")
    expected_providers = {member.value for member in ExecutionProvider}
    if declared_provider in expected_providers and response.execution_provider != declared_provider:
        print(
            f"scorer healthcheck: execution provider is {response.execution_provider} but this task "
            f"requested {declared_provider} — FAILED DEPLOY, not a degradation (rules.md R-45)",
            file=sys.stderr,
        )
        return 1

    if response.artifact_state == ArtifactState.POLICY_ELIGIBLE.value and (
        not response.contract_vector_parity_ok
    ):
        # Unreachable through `_supported_artifact_state`, which caps the state at DEMO_ELIGIBLE whenever
        # parity is unverified. Asserted anyway: this is the one combination that would let an off-contract
        # build be described as policy-eligible, and the cost of a tripwire on an invariant that is
        # currently structural is four lines.
        print(
            "scorer healthcheck: artifact_state claims policy_eligible with UNVERIFIED contract "
            "vector parity; the two cannot both be true (frame_contract.md §6)",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["--healthcheck"]:
        return healthcheck()
    if args:
        print("usage: python -m app.server [--healthcheck]", file=sys.stderr)
        return 64  # EX_USAGE
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
