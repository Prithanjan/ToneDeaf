"""Configuration: the tier is a value (R-04), the Scorer holds no secrets (R-34), and it fails fast.

Each refusal below prevents a degradation that is SILENT. The service would come up, score windows,
return plausible numbers, and pass its health check — while invalidating something that is going to be
shown to a judge.
"""

from __future__ import annotations

import pytest

from app.config import (
    ARTIFACT_STATE_RANK,
    DEFAULT_GRPC_PORT,
    FORBIDDEN_FIELD_SUBSTRINGS,
    ArtifactState,
    ConfigError,
    DeploymentProfile,
    DetectorMode,
    ExecutionProvider,
    field_names,
    load_settings,
)


class TestNoSecrets:
    """rules.md R-34. The Scorer needs no key material and must never be given any."""

    def test_no_config_field_name_suggests_a_secret(self) -> None:
        """Prevents the HMAC key's blast radius silently widening to a second service.

        The Scorer does not pseudonymize (``session_ref`` arrives already pseudonymized, rules.md R-16),
        does not sign tickets, and does not write audit rows — so it needs no key, and a task definition
        that injects one is misconfigured. Asserted as a property of the field set so a future
        ``hmac_key`` field fails a test instead of quietly becoming normal.
        """
        for name in field_names():
            tokens = set(name.split("_"))
            for forbidden in FORBIDDEN_FIELD_SUBSTRINGS:
                assert forbidden not in tokens, f"{name} looks like a secret"
                assert forbidden not in name, f"{name} looks like a secret"

    def test_field_set_is_what_the_service_actually_needs(self) -> None:
        """Prevents unused configuration accumulating, which is where a secret eventually gets parked."""
        assert set(field_names()) == {
            "deployment_profile",
            "execution_provider",
            "detector_mode",
            "artifact_state",
            "model_path",
            "calibration_path",
            "contract_vector_path",
            "grpc_port",
            "grpc_max_workers",
            "grpc_max_concurrent_rpcs",
            "ort_intra_op_threads",
            "contract_vector_atol",
            "log_level",
            "git_commit",
        }


class TestTierIsAValueNotABranch:
    """rules.md R-04. What makes the CPU fallback a parity tier rather than a second implementation."""

    def test_both_profiles_load(self, base_env: dict[str, str]) -> None:
        local = load_settings(env=base_env)
        assert local.deployment_profile is DeploymentProfile.LOCAL_CPU

        aws = dict(base_env)
        aws.update(
            {
                "DEPLOYMENT_PROFILE": "aws-gpu",
                "EXECUTION_PROVIDER": "CUDAExecutionProvider",
                "DETECTOR_MODE": "REAL_DETECTOR",
            }
        )
        assert load_settings(env=aws).deployment_profile is DeploymentProfile.AWS_GPU

    def test_no_serving_module_branches_on_the_profile(self) -> None:
        """Prevents the two tiers becoming two implementations, which would make parity unmeasurable.

        The profile is read once, in validation. If a serving path branched on it, "the same trace on both
        tiers" would be a claim about two different code paths — and the Day-5 parity demo would be
        comparing a system to itself only by coincidence.

        Asserted on the ``DeploymentProfile`` symbol rather than on the string values, because the strings
        legitimately appear in prose that explains why the branch does not exist. The symbol is what a
        branch would need.
        """
        from pathlib import Path

        from app import config, contract, model, server

        for module in (server, model, contract):
            source = Path(module.__file__).read_text(encoding="utf-8")
            assert "DeploymentProfile" not in source, (
                f"{module.__name__} references the deployment tier enum; the tier is a value, not a "
                "branch (rules.md R-04)"
            )
        # config.py is the one module allowed to use it: that is where the value is parsed and validated.
        assert "DeploymentProfile" in Path(config.__file__).read_text(encoding="utf-8")


class TestFailFastValidation:
    """Refuse to start. Every alternative here is a silent degradation."""

    def test_gpu_profile_rejects_a_cpu_provider(self, base_env: dict[str, str]) -> None:
        """rules.md R-45. Prevents a whole day of latency numbers describing the wrong machine.

        This is the configuration-level half of the guard; ``model.py``'s runtime assertion is the other
        half. Both are needed: this one catches a wrong task definition before the container even loads
        ORT, and that one catches a correct task definition on a host where CUDA fails to initialize.
        """
        env = dict(base_env)
        env.update({"DEPLOYMENT_PROFILE": "aws-gpu", "EXECUTION_PROVIDER": "CPUExecutionProvider"})
        with pytest.raises(ConfigError, match="R-45"):
            load_settings(env=env)

    def test_gpu_profile_rejects_mock_mode(self, base_env: dict[str, str]) -> None:
        """rules.md R-32 and R-46. Prevents the project's single GPU being billed to run a hash.

        Exactly one ``g4dn.xlarge`` is permitted for the whole five-day window. Running mock mode on it
        produces latency numbers that describe a BLAKE2b digest, and those numbers would be recorded under
        the GPU tier's name in ``evaluation/reports/``. It is the same shape of refusal as the Gateway
        declining to reach its local test issuer under ``aws-gpu``: a demo harness must not be reachable
        from a production-shaped deployment.
        """
        env = dict(base_env)
        env.update(
            {
                "DEPLOYMENT_PROFILE": "aws-gpu",
                "EXECUTION_PROVIDER": "CUDAExecutionProvider",
                "DETECTOR_MODE": "MOCK_SMOKE_MODE_NOT_A_DETECTOR",
            }
        )
        with pytest.raises(ConfigError, match="R-32"):
            load_settings(env=env)

    def test_mock_mode_refuses_a_policy_eligible_manifest(self, base_env: dict[str, str]) -> None:
        """technical-design.md §7 and rules.md R-46, verbatim.

        This is the single most important refusal in the service. A mock score presented as a
        policy-eligible measurement is a false capability claim that survives into a slide, and unlike a
        wrong number it is not something a reviewer can spot by looking at the output.
        """
        env = dict(base_env)
        env["ARTIFACT_STATE"] = "policy_eligible"
        with pytest.raises(ConfigError, match="R-46"):
            load_settings(env=env)

    @pytest.mark.parametrize(
        "name", ["DEPLOYMENT_PROFILE", "EXECUTION_PROVIDER", "DETECTOR_MODE", "LOG_LEVEL"]
    )
    def test_required_variables_have_no_default(self, base_env: dict[str, str], name: str) -> None:
        """Prevents a partial configuration being completed by a guess.

        ``DETECTOR_MODE`` in particular has no default in either direction. Defaulting to mock would let a
        real deploy quietly serve hashes; defaulting to real would make ``docker compose up`` fail on a
        missing model file with a stack trace instead of a configuration error.
        """
        env = dict(base_env)
        del env[name]
        if name == "LOG_LEVEL":
            # LOG_LEVEL has a documented default; unsetting it must still produce a valid object.
            assert load_settings(env=env).log_level == "INFO"
            return
        with pytest.raises(ConfigError):
            load_settings(env=env)

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("DEPLOYMENT_PROFILE", "aws"),
            ("EXECUTION_PROVIDER", "cuda"),
            ("DETECTOR_MODE", "mock"),
            ("ARTIFACT_STATE", "eligible"),
            ("LOG_LEVEL", "TRACE"),
            ("GRPC_PORT", "not-a-number"),
            ("GRPC_MAX_WORKERS", "0"),
            ("GRPC_MAX_CONCURRENT_RPCS", "0"),
            ("ORT_INTRA_OP_THREADS", "0"),
            ("CONTRACT_VECTOR_ATOL", "0"),
            ("CONTRACT_VECTOR_ATOL", "2"),
            ("GRPC_PORT", "70000"),
        ],
    )
    def test_invalid_values_are_refused(
        self, base_env: dict[str, str], name: str, value: str
    ) -> None:
        env = dict(base_env)
        env[name] = value
        with pytest.raises(ConfigError):
            load_settings(env=env)

    def test_detector_mode_short_spelling_is_not_accepted(self, base_env: dict[str, str]) -> None:
        """rules.md R-46. Prevents the label being abbreviated at the configuration boundary.

        The long spelling is the label that appears in every log line and every response. Accepting
        ``mock`` here would create a second, skimmable name for the same state.
        """
        env = dict(base_env)
        env["DETECTOR_MODE"] = "MOCK"
        with pytest.raises(ConfigError):
            load_settings(env=env)

    def test_gpu_tier_leaves_thread_count_unset(self, base_env: dict[str, str]) -> None:
        """technical-design.md §8: ``ORT_INTRA_OP_THREADS`` is unset on GPU.

        A measured p95 belongs to a named host (rules.md R-47), so a thread count from the CPU sweep must
        not be baked into the image and applied to a GPU session where it means something different.
        """
        env = dict(base_env)
        env.update(
            {
                "DEPLOYMENT_PROFILE": "aws-gpu",
                "EXECUTION_PROVIDER": "CUDAExecutionProvider",
                "DETECTOR_MODE": "REAL_DETECTOR",
            }
        )
        assert load_settings(env=env).ort_intra_op_threads is None


class TestArtifactState:
    """Derived, never declared — the same rule ``gateway/app/policy/loader.py`` applies to its bundle."""

    def test_declared_state_is_capped_at_what_artifacts_support(
        self, base_env: dict[str, str]
    ) -> None:
        """Prevents a manifest claim outranking the artifacts that are actually loaded."""
        env = dict(base_env)
        env["ARTIFACT_STATE"] = "demo_eligible"
        settings = load_settings(env=env)
        assert (
            settings.capped_artifact_state(supported=ArtifactState.RESEARCH_ONLY)
            is ArtifactState.RESEARCH_ONLY
        )

    def test_understatement_is_honoured_not_promoted(self, base_env: dict[str, str]) -> None:
        """The deliberate asymmetry, stated as a test.

        Overstatement is a false claim and is refused outright in validation. Understatement is a judgement
        call someone is entitled to make — ``research_only`` on perfectly good artifacts might reflect a
        review that has not happened yet — and silently promoting it would overrule them.
        """
        settings = load_settings(env=base_env)  # declares research_only
        assert (
            settings.capped_artifact_state(supported=ArtifactState.POLICY_ELIGIBLE)
            is ArtifactState.RESEARCH_ONLY
        )

    def test_rank_is_total_and_ordered(self) -> None:
        assert (
            ARTIFACT_STATE_RANK[ArtifactState.RESEARCH_ONLY]
            < ARTIFACT_STATE_RANK[ArtifactState.DEMO_ELIGIBLE]
            < ARTIFACT_STATE_RANK[ArtifactState.POLICY_ELIGIBLE]
        )
        assert set(ARTIFACT_STATE_RANK) == set(ArtifactState)


class TestEnumValues:
    """Enum string values cross service and artifact boundaries; they are contract, not convenience."""

    def test_provider_values_match_onnxruntime_spelling(self) -> None:
        """Prevents the Gateway's exact-string comparison failing on a cosmetic difference.

        ``gateway/app/main.py`` compares ``scorer_health.execution_provider`` against its own configured
        value with ``!=``. Any difference in spelling — including case — is a startup refusal.
        """
        assert ExecutionProvider.CPU.value == "CPUExecutionProvider"
        assert ExecutionProvider.CUDA.value == "CUDAExecutionProvider"

    def test_detector_mode_values_match_the_proto_enum_names(self) -> None:
        """Prevents a translation table existing between the config value and the wire value.

        The values are identical to the proto's enum NAMES, so ``pb.DetectorMode.Value(mode.value)`` is the
        whole conversion. A mapping dict would be a second place for the two to drift apart.
        """
        from app import voice_scorer_pb2 as pb

        for mode in DetectorMode:
            assert pb.DetectorMode.Value(mode.value) >= 0

    def test_artifact_state_values_match_the_gateway(self) -> None:
        """The three states in ``gateway/app/policy/loader.py::VALID_ARTIFACT_STATES``."""
        from tests.conftest import REPO_ROOT

        loader = (REPO_ROOT / "gateway" / "app" / "policy" / "loader.py").read_text(
            encoding="utf-8"
        )
        for state in ArtifactState:
            assert f'"{state.value}"' in loader

    def test_default_port_is_the_documented_one(self) -> None:
        assert DEFAULT_GRPC_PORT == 50_051
