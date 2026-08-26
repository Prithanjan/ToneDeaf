"""Detectors: the ONNX Runtime session, the execution-provider assertion, and mock mode.

TWO THINGS IN THIS FILE ARE LOAD-BEARING FOR THE PROJECT'S CLAIM INTEGRITY.

**1. A mock score must never be mistakable for a measurement.**
This is the most dangerous failure mode in the service. A wrong score is a bug someone finds; a mock
score presented as a real one is a false claim that survives all the way into a slide. So
:class:`MockDetector` is not a "detector with a stub model" — it is a separate class that reports a
different ``model_version``, a different ``detector_mode``, and a ``model_sha256`` of the empty string,
and there is no configuration that makes it report ``REAL_DETECTOR``. Every response, the health
response, the banner, and the per-window log line carry the mode
(``MOCK_SMOKE_MODE_NOT_A_DETECTOR``, spelled the long way so a reader's eye cannot skip it — rules.md
R-46).

**2. A silent CPU fallback on the GPU tier is a FAILED DEPLOY, not a degraded one (rules.md R-45).**
Why "healthy but silently on CPU" is worse than a crash:

A crash is loud, immediate, attributable, and self-limiting. Someone reads the exit reason, fixes the
task definition, and no number recorded before the fix is trusted, because there are none. A silent
fallback is the opposite on every axis. The task passes its health check, the service scores every
window correctly, the Gateway starts, the PWA gets risk events, and the demo works — so nobody looks.
Meanwhile:

* Every p95 recorded that day describes a CPU running a graph sized for a GPU. Those numbers go into
  ``evaluation/reports/``, into the presentation, and into the acceptance measures. They are not
  conservative estimates of GPU latency; they are measurements of a different system, and they are
  wrong in the direction that makes the architecture look unnecessary.
* ``execution_provider`` is a column in the audit table and a field in the parity set
  (architecture.md §5.1). A fallback writes ``CUDAExecutionProvider`` next to CPU timings, so the
  audit trail — the artifact whose entire purpose is to be trustworthy — records something false.
* The Day-5 dual-tier parity claim becomes untestable: both tiers were CPU, so "same trace on both
  providers" was never actually exercised, and the one test that would have caught it passed.
* It is discovered late, under demo pressure, by someone who has no reason to suspect the provider —
  because the symptom is "it's a bit slow", which has fifty plausible causes.

The cost of the crash is minutes. The cost of the fallback is every latency claim in the deliverable,
plus the credibility of the audit trail that was supposed to be the reason to believe the rest. Hence
:class:`ProviderUnavailable` is raised before the server binds a port, and the service never reaches a
state where it could answer ``ready = true`` on the wrong provider.
"""

from __future__ import annotations

import hashlib
import math
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Final

import numpy as np

from app.config import ExecutionProvider, ScorerSettings
from app.contract import ONNX_INPUT_SHAPE

#: Reported by MockDetector. Contains the words that make it unusable as a real version string: no
#: release manifest, slide, or metric label carrying this value can be mistaken for a model identity.
MOCK_MODEL_VERSION: Final[str] = "mock-smoke-not-a-detector"

#: SHA-256 of the empty byte string. Truthful (there is no model file) and structurally
#: distinguishable from any real artifact hash, while still being a well-formed 64-hex-char value so
#: the Gateway's hash-comparison in ``gateway/app/main.py`` compares two defined things.
EMPTY_SHA256: Final[str] = hashlib.sha256(b"").hexdigest()

#: The mock's raw score is generated as a LOGIT, not as a probability, so that the Platt transform in
#: ``calibration.py`` is genuinely exercised on the mock path — and so that the calibrated output
#: spans enough of [0,1] to drive the Gateway's k-of-n state machine through all three risk states.
#: A mock that emitted, say, [0,1] pre-sigmoid would compress to [0.5, 0.73] after calibration and
#: could never cross a 0.78 threshold: Pair C's policy wiring would look correct and be untested.
MOCK_LOGIT_RANGE: Final[float] = 6.0

#: Domain separation for the mock's digest. Prevents this hash from ever colliding with a
#: pseudonymization or ticket HMAC computed elsewhere in the system over the same bytes.
_MOCK_DIGEST_DOMAIN: Final[bytes] = b"sih26104/scorer/mock-smoke-v1"


class ModelLoadError(RuntimeError):
    """The model artifact is missing, unreadable, or not the one the calibration was fitted for."""


class ProviderUnavailable(RuntimeError):
    """The requested ONNX Runtime execution provider is not the one that would actually run.

    Raised before the gRPC port is bound. See the module docstring for why this is fatal rather than a
    warning (rules.md R-45).
    """


class Detector(ABC):
    """What ``server.py`` is allowed to know about a scorer.

    Deliberately narrow. There is no method here that takes a ``purpose_code``, a session history, or
    anything else policy-shaped, and no method that returns an action — the detection/decision seam is
    structural in the proto (``contracts/voice_scorer.proto``) and this interface does not widen it.

    :meth:`score` returns a PRE-calibration raw value. Calibration is applied by the caller, from a
    separately-versioned artifact, so that swapping the calibration never means touching a detector and
    ``raw_score`` stays available for the ONNX parity gate (frame_contract.md §6).

    ``__slots__ = ()`` is not cosmetic. Both implementations declare ``__slots__``, and that declaration
    only removes the instance ``__dict__`` if every class in the MRO does the same — an unslotted base
    silently restores it. Without this line the R-14 claim in ``MockDetector`` ("there is nowhere to keep
    a buffer") would be decoration: ``detector.last_window = window`` would succeed. With it, that
    assignment raises ``AttributeError``, so a future edit that tried to stash 2.56 seconds of a
    caller's voice on the detector fails at the point of the edit rather than in a privacy review.
    """

    __slots__ = ()

    @property
    @abstractmethod
    def model_version(self) -> str: ...

    @property
    @abstractmethod
    def model_sha256(self) -> str: ...

    @property
    @abstractmethod
    def execution_provider(self) -> str:
        """The provider ACTUALLY in use, never the one requested (proto ``HealthResponse`` §2)."""

    @abstractmethod
    def score(self, window: np.ndarray, *, session_ref: str) -> float:
        """Score one ``(1, 40960)`` float32 window. Returns a pre-calibration raw score."""

    def close(self) -> None:
        """Release the runtime session. Deliberately CONCRETE, not abstract.

        ``MockDetector`` holds no session, no file handle, and no device memory — there is nothing for it
        to release, and forcing it to declare an empty override would add a method whose only content is
        the absence of content. ``OnnxDetector`` overrides this with a real teardown.

        The B027 lint that flags an un-decorated empty method in an ABC is asking a fair question, and
        the answer is that shutdown must not depend on which detector is loaded: ``server.py`` calls
        ``runtime.detector.close()`` on one unconditional path. If this were abstract, a third
        implementation added later could satisfy the type checker with ``pass`` and nobody would notice;
        as a documented no-op base, the only implementation that needs teardown is the one that has
        something to tear down.
        """
        return None


class MockDetector(Detector):
    """Deterministic pseudo-scores. NOT A DETECTOR. Transport smoke testing only.

    Exists because the Gateway, the Compose tier, and CI all have to work before any model does:
    ``policy/calibration.json`` and ``ml/aasist.onnx`` are Pair B's Phase-2/3 deliverables, while the
    Compose stack and the Gateway's WSS contract tests are Phase-1 exit criteria (phases.md §2.1,
    §2.4). Without this class, Pair A's Gateway wiring and Pair C's integration harness would both be
    blocked on an ML artifact two days out.

    DETERMINISM, AND WHAT IT IS DERIVED FROM
    The score is a BLAKE2b digest over ``session_ref`` and the window's PCM bytes. Two properties
    follow, and both are needed:

    * Same audio + same session ⇒ same score, in every process, on every host, across restarts. So a
      replayed test call produces an identical policy trace, which is what makes the dual-tier parity
      rehearsal meaningful even while both tiers are mocked. ``hash()`` would have been wrong here:
      it is salted per process, so two runs would disagree and the disagreement would look like a
      parity failure.
    * Different windows in the same session get different scores. ``technical-design.md`` §7 says the
      sequence is derived "from ``session_ref``"; derived from ``session_ref`` ALONE, every window in a
      session would score identically, so the k-of-n evidence rule could only ever produce all-high or
      all-low and Pair C's state machine would be wired but unexercised. Mixing in the window content
      keeps the letter of "deterministic from ``session_ref``" — the sequence is reproducible for a
      given session and its audio — while making the sequence a sequence.

    ``window_seq`` is deliberately NOT an input. The proto states the Scorer is stateless and must not
    use ``window_seq`` to correlate windows; keeping it out of the score function means this class
    cannot develop a dependence on window ordering that a real detector would not have.

    NO AUDIO IS RETAINED (rules.md R-14). The digest is computed and the buffer is dropped when the
    request scope ends. A digest is not audio and is never logged, persisted, or returned.
    """

    __slots__ = ("_provider",)

    def __init__(self, *, reported_provider: ExecutionProvider):
        # Mock mode runs no ONNX session, so there is no provider to observe. The CONFIGURED value is
        # reported instead, for one specific reason: `gateway/app/main.py` refuses to start when the
        # Scorer's provider does not equal its own configured provider, so returning a token like
        # "MOCK_NO_PROVIDER" would make the mandatory Compose tier unstartable. The honesty is
        # preserved by pairing: this field never travels without `detector_mode` in the same message,
        # and the Gateway banner renders mock mode as a two-line warning block. On the GPU tier the
        # question cannot arise — config.py refuses to start mock mode under aws-gpu at all.
        self._provider = reported_provider.value

    @property
    def model_version(self) -> str:
        return MOCK_MODEL_VERSION

    @property
    def model_sha256(self) -> str:
        return EMPTY_SHA256

    @property
    def execution_provider(self) -> str:
        return self._provider

    def score(self, window: np.ndarray, *, session_ref: str) -> float:
        """A reproducible pseudo-logit in ``(-6, +6)``, derived from the session and the audio."""
        if window.shape != ONNX_INPUT_SHAPE:
            raise ValueError("window must have shape (1, 40960)")

        digest = hashlib.blake2b(
            _MOCK_DIGEST_DOMAIN,
            digest_size=8,
            key=session_ref.encode("utf-8")[:64],
        )
        # np.ascontiguousarray keeps the digest independent of how the array was produced (a view, a
        # slice, a reshape) so the same samples always hash the same way.
        digest.update(np.ascontiguousarray(window, dtype=np.float32).tobytes())

        # Map the 64-bit digest onto (0,1) then onto a logit range. The +0.5 offset keeps the value
        # strictly inside the open interval, so the logit never saturates to exactly ±inf territory.
        unit = (int.from_bytes(digest.digest(), "big") + 0.5) / 2**64
        return float(MOCK_LOGIT_RANGE * (2.0 * unit - 1.0))


class OnnxDetector(Detector):
    """An ONNX Runtime session over ``aasist.onnx``, with the provider assertion from rules.md R-45.

    ``onnxruntime`` is imported lazily inside :meth:`load` rather than at module import. Mock mode is
    mandatory and must work with no ONNX Runtime present at all — a top-level import would make the
    unit suite and the Phase-1 Compose tier depend on a wheel that only the serving image needs, and
    would make ``import app.server`` fail on a developer laptop.
    """

    __slots__ = ("_input_name", "_model_sha256", "_model_version", "_path", "_provider", "_session")

    def __init__(self, *, path: Path, model_version: str):
        self._path = path
        self._model_version = model_version
        self._session: Any | None = None
        self._input_name: str = ""
        self._provider: str = ""
        self._model_sha256: str = ""

    # -- identity ----------------------------------------------------------------------------------

    @property
    def model_version(self) -> str:
        return self._model_version

    @property
    def model_sha256(self) -> str:
        return self._model_sha256

    @property
    def execution_provider(self) -> str:
        return self._provider

    # -- lifecycle ---------------------------------------------------------------------------------

    def load(self, settings: ScorerSettings) -> None:
        """Hash the artifact, create the session, assert the provider, warm up. Fails fast.

        Order matters. The hash is taken from the bytes on disk BEFORE the session exists, so the
        value reported in the parity set describes the file rather than whatever the runtime decided to
        do with it. The provider assertion happens before warmup so a fallback is reported as a
        provider error, not as a slow first inference.
        """
        try:
            # Deliberately lazy, not a stray local import; see the class docstring.
            import onnxruntime as ort
        except ImportError as exc:
            raise ModelLoadError(
                "onnxruntime is not installed. The serving image installs exactly one wheel "
                "(onnxruntime or onnxruntime-gpu, selected by the ORT_PACKAGE build arg in "
                "scorer/Dockerfile). To run without a model, set "
                "DETECTOR_MODE=MOCK_SMOKE_MODE_NOT_A_DETECTOR."
            ) from exc

        if not self._path.is_file():
            raise ModelLoadError(
                f"model artifact not found: {self._path}. A real detector with no model is not a "
                "degraded detector; set DETECTOR_MODE=MOCK_SMOKE_MODE_NOT_A_DETECTOR to run the "
                "transport smoke path instead."
            )

        self._model_sha256 = _sha256_file(self._path)

        requested = settings.execution_provider.value
        available = tuple(ort.get_available_providers())
        if requested not in available:
            # The wrong wheel: onnxruntime (CPU-only) installed on a task definition that asks for
            # CUDA. Caught here rather than at the first inference, where it would surface as a
            # silent fallback (rules.md R-45).
            raise ProviderUnavailable(
                f"{requested} is not registered in this ONNX Runtime build. Available: "
                f"{list(available)}. On the GPU tier this means the CPU wheel was installed, or the "
                "CUDA/cuDNN runtime libraries are not on the library path."
            )

        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        if settings.ort_intra_op_threads is not None:
            # From the measured CPU thread sweep. A measured p95 belongs to a named host (rules.md
            # R-47), so this is config, never a guess baked into the image.
            options.intra_op_num_threads = settings.ort_intra_op_threads

        # Only the requested provider is offered. Passing a fallback list is exactly how a silent CPU
        # fallback happens: ORT would accept ["CUDAExecutionProvider", "CPUExecutionProvider"], fail
        # to initialize CUDA, log a warning nobody reads, and serve on CPU while the task stays
        # healthy. With a single-entry list, failure to initialize is an exception here.
        self._session = ort.InferenceSession(
            str(self._path), sess_options=options, providers=[requested]
        )

        self.assert_provider(requested)

        inputs = self._session.get_inputs()
        if len(inputs) != 1:
            raise ModelLoadError("the exported graph must take exactly one input (playbook §7)")
        self._input_name = inputs[0].name
        _assert_input_shape(inputs[0].shape)

        # Warmup. The first CUDA inference pays kernel compilation and allocator setup; leaving that
        # inside the first real request would attribute tens of milliseconds of one-time cost to the
        # first window of the first session, and that window's latency would then be reported as a
        # cold-start p99 for the whole system.
        self.score(np.zeros(ONNX_INPUT_SHAPE, dtype=np.float32), session_ref="warmup")

    def assert_provider(self, requested: str) -> None:
        """Assert the provider ACTUALLY in use is the one requested. Raises, never warns.

        ``get_providers()`` returns the session's registered providers in priority order, so index 0 is
        the one that will claim every node it supports. Checking membership instead of position would
        pass on a session that registered CUDA and then ran everything on the CPU fallback — which is
        precisely the state this assertion exists to reject.
        """
        if self._session is None:
            raise ProviderUnavailable("no ONNX Runtime session; the model was never loaded")

        active = tuple(self._session.get_providers())
        if not active or active[0] != requested:
            raise ProviderUnavailable(
                f"execution provider fell back: requested {requested}, session resolved to "
                f"{list(active)}. This is a FAILED DEPLOY, not a degradation: every latency number "
                "recorded on this host would describe a different system, and execution_provider is "
                "written into every audit row (rules.md R-45)."
            )
        self._provider = active[0]

    def close(self) -> None:
        self._session = None

    # -- inference ---------------------------------------------------------------------------------

    def score(self, window: np.ndarray, *, session_ref: str) -> float:
        """Run the graph. ``session_ref`` is unused — a real detector is stateless per window.

        The parameter exists to keep one :class:`Detector` signature across both implementations. If it
        were absent here, ``server.py`` would have to branch on the detector type on the hot path, and
        that branch is where a mock would eventually acquire a different code path from a real model.
        """
        if self._session is None:
            raise ModelLoadError("model is not loaded")
        if window.shape != ONNX_INPUT_SHAPE:
            raise ValueError("window must have shape (1, 40960)")
        if window.dtype != np.float32:
            # ORT would raise its own error, but with a message about a tensor type mismatch that does
            # not name the contract. float64 here means someone dropped the astype in the conversion.
            raise ValueError(
                "window must be float32 (contract.py::pcm16_to_float32 guarantees this)"
            )

        outputs = self._session.run(None, {self._input_name: window})
        return _extract_raw_score(outputs)


def _sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    """Stream the hash. The ONNX file is tens of megabytes; reading it whole to hash it would double
    the container's peak RSS at startup for no reason."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def _assert_input_shape(shape: list[Any]) -> None:
    """The exported graph must declare ``[1, 40960]`` (frame_contract.md §1, playbook §7).

    A symbolic dimension is accepted: exporters commonly emit a named dynamic axis for the batch, and
    rejecting that would fail on a graph that is correct. A *concrete* dimension that disagrees is
    refused — that is an export against a different window length, and it would be fed 2.56 s of audio
    reinterpreted as something else.
    """
    if len(shape) != len(ONNX_INPUT_SHAPE):
        raise ModelLoadError("the exported graph must declare a 2-D input of shape [1, 40960]")
    for actual, expected in zip(shape, ONNX_INPUT_SHAPE, strict=True):
        if isinstance(actual, int) and actual != expected:
            raise ModelLoadError(
                "the exported graph's input shape does not match the window contract [1, 40960]"
            )


def _extract_raw_score(outputs: list[Any]) -> float:
    """Pull one scalar raw score out of the graph's output.

    Accepts a scalar, a ``(1,)`` vector, or a ``(1, 1)`` matrix — the three shapes a single-logit
    export actually produces. A two-class output is REFUSED rather than reduced: choosing between
    ``out[1]``, ``out[1] - out[0]``, and ``softmax(out)[1]`` is a class-orientation decision, and
    playbook §7 requires orientation to be explicit at export. Guessing it here is how a detector ends
    up inverted while every shape check passes.
    """
    if not outputs:
        raise ModelLoadError("the exported graph produced no output")
    array = np.asarray(outputs[0], dtype=np.float64).reshape(-1)
    if array.size != 1:
        raise ModelLoadError(
            "the exported graph must produce exactly one scalar raw score. A multi-class output "
            "requires an explicit orientation decision at export time (playbook §7), not a reduction "
            "chosen at serving time."
        )
    value = float(array[0])
    if not math.isfinite(value):
        raise ModelLoadError("the model produced a non-finite raw score")
    return value


def build_detector(settings: ScorerSettings, *, model_version: str) -> Detector:
    """Construct the detector the configuration asks for. The ONE place the mode is decided.

    A single construction site means there is no code path that produces a detector whose reported
    mode disagrees with :attr:`ScorerSettings.detector_mode`, and ``server.py`` never needs to know
    which kind it holds.
    """
    if settings.is_mock:
        return MockDetector(reported_provider=settings.execution_provider)

    detector = OnnxDetector(path=settings.model_path, model_version=model_version)
    detector.load(settings)
    return detector
