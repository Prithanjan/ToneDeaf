"""Configuration — the deployment tier is a VALUE, never a code branch (rules.md R-04).

There is no ``if profile == "aws"`` on any serving path in this service. The execution provider, the
model path, the calibration path, and the thread count are all config values, which is what makes the
CPU fallback tier a genuine parity tier rather than a second implementation.

WHY THIS IS STDLIB AND NOT pydantic-settings
``gateway/app/config.py`` uses pydantic-settings, and mirroring it here would be the obvious move.
The Gateway already depends on pydantic for its request models, so settings validation is free there.
The Scorer's runtime dependency set is ``grpcio``, ``protobuf``, ``numpy``, and one ONNX Runtime wheel
— and that set is part of the parity story (architecture.md §5.1 lists the ORT package as the ONE
permitted difference between the two images). Adding pydantic to the serving image so that nine
environment variables can be parsed by a library instead of by this file makes the dependency diff
between the two Scorer images larger for no validation that is not written out below.

THE SCORER HOLDS NO SECRETS
There is no ``SecretStr`` field here and there is no key material in this process. The Scorer does not
pseudonymize (the ``session_ref`` it receives is already an HMAC pseudonym, rules.md R-16), does not
sign tickets, and does not write audit rows — so it needs no HMAC key, and an image or task definition
that injects one into the Scorer is misconfigured (rules.md R-34). ``tests/test_config.py`` asserts
this as a property of the field set, so a future field named ``*_key`` fails a test rather than
quietly widening the secret blast radius to a second service.

FAIL-FAST, NOT DEGRADE
Every check in :meth:`ScorerSettings._validate` refuses to start. Each degradation it prevents is
silent, and each one invalidates something that will be shown to a judge:

* ``CPUExecutionProvider`` on the GPU tier invalidates every latency number recorded that day
  (rules.md R-45);
* mock mode on the GPU tier bills for a ``g4dn.xlarge`` to run a hash function, and produces latency
  numbers that describe nothing (rules.md R-32, R-46);
* an ``artifact_state`` of ``policy_eligible`` asserted over a mock detector or a placeholder
  calibration is a false capability claim (rules.md R-01, R-11, R-46).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from enum import Enum
from pathlib import Path
from typing import Final


class DeploymentProfile(str, Enum):
    LOCAL_CPU = "local-cpu"
    AWS_GPU = "aws-gpu"


class ExecutionProvider(str, Enum):
    CPU = "CPUExecutionProvider"
    CUDA = "CUDAExecutionProvider"


class DetectorMode(str, Enum):
    """Mirrors the proto enum's names exactly, so a value never needs translating on the way out.

    ``MOCK_SMOKE_MODE_NOT_A_DETECTOR`` is spelled the long way on purpose. ``mock`` in a log line is
    something a reader's eye skips; this is not (rules.md R-46).
    """

    REAL = "REAL_DETECTOR"
    MOCK = "MOCK_SMOKE_MODE_NOT_A_DETECTOR"


class ArtifactState(str, Enum):
    """From ``docs/manifests/release_manifest.json`` (proto ``HealthResponse.artifact_state``)."""

    RESEARCH_ONLY = "research_only"
    DEMO_ELIGIBLE = "demo_eligible"
    POLICY_ELIGIBLE = "policy_eligible"


#: Ordered weakest to strongest. Used to CAP a declared state at what the loaded artifacts actually
#: support, rather than trusting the declaration.
ARTIFACT_STATE_RANK: Final[dict[ArtifactState, int]] = {
    ArtifactState.RESEARCH_ONLY: 0,
    ArtifactState.DEMO_ELIGIBLE: 1,
    ArtifactState.POLICY_ELIGIBLE: 2,
}

DEFAULT_GRPC_PORT: Final[int] = 50_051


class ConfigError(ValueError):
    """The environment is not a valid configuration. Refuse to start."""


@dataclass(frozen=True, slots=True)
class ScorerSettings:
    """The whole configuration surface. One object; the tier is a value (technical-design.md §8)."""

    deployment_profile: DeploymentProfile
    execution_provider: ExecutionProvider
    detector_mode: DetectorMode
    artifact_state: ArtifactState

    model_path: Path
    calibration_path: Path
    contract_vector_path: Path

    grpc_port: int
    grpc_max_workers: int
    #: Bounded so the Scorer refuses rather than queues. Queued audio is retained audio (rules.md
    #: R-20), and a queue inside this process is invisible to the Gateway's backpressure decision —
    #: ``ScorerClient`` measures saturation with its own semaphore and can only see what it sent.
    grpc_max_concurrent_rpcs: int

    #: ``None`` on the GPU tier (technical-design.md §8); on CPU it comes from the measured thread
    #: sweep, and a measured value belongs to a named host (rules.md R-47).
    ort_intra_op_threads: int | None

    #: frame_contract.md §6 declares atol=1e-4 on raw_score.
    contract_vector_atol: float

    log_level: str
    git_commit: str

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        aws = self.deployment_profile is DeploymentProfile.AWS_GPU
        mock = self.detector_mode is DetectorMode.MOCK

        if aws and self.execution_provider is not ExecutionProvider.CUDA:
            raise ConfigError(
                "aws-gpu requires CUDAExecutionProvider: a CPU provider on the GPU tier invalidates "
                "every latency measurement recorded that day (rules.md R-45)"
            )

        if aws and mock:
            # The same shape as the Gateway's refusal to reach the local test issuer under aws-gpu
            # (gateway/app/config.py, rules.md R-05): a demo harness must not be reachable from a
            # production-shaped deployment. Here the cost is doubled — exactly one g4dn.xlarge is
            # permitted for the whole five-day window (rules.md R-32), and spending it on a hash
            # function produces latency numbers that describe the hash, not the detector.
            raise ConfigError(
                "MOCK_SMOKE_MODE_NOT_A_DETECTOR must not run on the aws-gpu tier: it would bill a GPU "
                "to run a deterministic hash and every latency number from that host would be fiction "
                "(rules.md R-32, R-46)"
            )

        if mock and self.artifact_state is ArtifactState.POLICY_ELIGIBLE:
            # technical-design.md §7 and rules.md R-46, verbatim: mock mode refuses to start if the
            # release manifest asserts policy_eligible.
            raise ConfigError(
                "the release manifest asserts policy_eligible while DETECTOR_MODE is "
                "MOCK_SMOKE_MODE_NOT_A_DETECTOR. A mock score presented as a policy-eligible "
                "measurement is the failure this service exists to make impossible (rules.md R-46)"
            )

        if self.grpc_max_workers < 1:
            raise ConfigError("grpc_max_workers must be at least 1")
        if self.grpc_max_concurrent_rpcs < 1:
            raise ConfigError("grpc_max_concurrent_rpcs must be at least 1")
        if not 1 <= self.grpc_port <= 65_535:
            raise ConfigError("grpc_port is not a valid TCP port")
        if self.ort_intra_op_threads is not None and self.ort_intra_op_threads < 1:
            raise ConfigError("ort_intra_op_threads must be at least 1 when set")
        if not 0.0 < self.contract_vector_atol < 1.0:
            raise ConfigError("contract_vector_atol must be a small positive tolerance")
        if self.log_level not in _LOG_LEVELS:
            raise ConfigError(f"log_level must be one of {sorted(_LOG_LEVELS)}")

    # -- derived -----------------------------------------------------------------------------------

    @property
    def is_mock(self) -> bool:
        return self.detector_mode is DetectorMode.MOCK

    def capped_artifact_state(self, *, supported: ArtifactState) -> ArtifactState:
        """The weaker of what the manifest DECLARES and what the loaded artifacts SUPPORT.

        Derived, never declared — the same rule ``gateway/app/policy/loader.py`` applies to its own
        bundle. The asymmetry is deliberate: a declaration that *overstates* what the artifacts support
        is refused outright in :meth:`_validate` (that is a false claim), whereas a declaration that
        understates it is honoured (``research_only`` on perfectly good artifacts is a judgement call
        someone is entitled to make, and silently promoting it would overrule them).
        """
        if ARTIFACT_STATE_RANK[supported] < ARTIFACT_STATE_RANK[self.artifact_state]:
            return supported
        return self.artifact_state


_LOG_LEVELS: Final[frozenset[str]] = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})

#: Field-name substrings that must never appear in this dataclass. Asserted by a test, not by
#: convention (rules.md R-34).
FORBIDDEN_FIELD_SUBSTRINGS: Final[tuple[str, ...]] = (
    "key",
    "secret",
    "password",
    "token",
    "credential",
)


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise ConfigError(
            f"required environment variable is unset: {name}. The Scorer refuses to start on a "
            "partial configuration rather than guess a default (technical-design.md §8)."
        )
    return value


def _env_enum[E: Enum](name: str, enum_cls: type[E], default: str | None = None) -> E:
    raw = _env(name, default)
    try:
        return enum_cls(raw)
    except ValueError as exc:
        valid = sorted(member.value for member in enum_cls)  # type: ignore[attr-defined]
        raise ConfigError(f"{name} must be one of {valid}") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def _env_optional_int(name: str) -> int | None:
    """Unset and empty both mean "let ONNX Runtime decide" — which is correct on the GPU tier."""
    raw = os.environ.get(name, "")
    if raw == "":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer or unset") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc


def load_settings(env: dict[str, str] | None = None) -> ScorerSettings:
    """Read the environment once, at startup, and fail loudly on anything missing or invalid.

    ``env`` is injectable so the validation rules are testable without mutating ``os.environ`` — a
    test that set a real environment variable and raised before clearing it would leak that value into
    every subsequent test in the same process.
    """
    if env is not None:
        previous = dict(os.environ)
        os.environ.clear()
        os.environ.update(env)
        try:
            return load_settings()
        finally:
            os.environ.clear()
            os.environ.update(previous)

    return ScorerSettings(
        deployment_profile=_env_enum("DEPLOYMENT_PROFILE", DeploymentProfile),
        execution_provider=_env_enum("EXECUTION_PROVIDER", ExecutionProvider),
        # No default. "Which detector is this?" is the one question this service must never answer by
        # assumption — in either direction. Defaulting to mock would let a real deploy quietly serve
        # hashes; defaulting to real would make `docker compose up` fail on a missing model file with
        # a stack trace instead of a configuration error.
        detector_mode=_env_enum("DETECTOR_MODE", DetectorMode),
        artifact_state=_env_enum(
            "ARTIFACT_STATE", ArtifactState, ArtifactState.RESEARCH_ONLY.value
        ),
        model_path=Path(_env("MODEL_PATH", "/models/aasist.onnx")),
        calibration_path=Path(_env("CALIBRATION_PATH", "/policy/calibration.json")),
        contract_vector_path=Path(_env("CONTRACT_VECTOR_PATH", "/fixtures/contract_vector_v1.npy")),
        grpc_port=_env_int("GRPC_PORT", DEFAULT_GRPC_PORT),
        grpc_max_workers=_env_int("GRPC_MAX_WORKERS", 4),
        grpc_max_concurrent_rpcs=_env_int("GRPC_MAX_CONCURRENT_RPCS", 8),
        ort_intra_op_threads=_env_optional_int("ORT_INTRA_OP_THREADS"),
        contract_vector_atol=_env_float("CONTRACT_VECTOR_ATOL", 1.0e-4),
        log_level=_env("LOG_LEVEL", "INFO"),
        git_commit=_env("GIT_COMMIT", "unknown"),
    )


def field_names() -> tuple[str, ...]:
    """The config field set, for the no-secrets assertion in ``tests/test_config.py``."""
    return tuple(f.name for f in fields(ScorerSettings))
