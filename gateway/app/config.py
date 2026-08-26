"""Configuration — the deployment tier is a VALUE, never a code branch (rules.md R-04).

There is no ``if profile == "aws"`` anywhere in application code. Provider, issuer, trust root,
storage backing, and reachability are all config. That is what makes the local CPU fallback a
genuine parity tier rather than a second implementation.

Fail-fast is deliberate. Every check in ``_validate`` refuses to start rather than degrade, because
each degradation it prevents is silent and each one invalidates something a judge will be shown:

* a missing secret would otherwise surface as an HMAC over an empty key;
* the local test issuer reachable under ``aws-gpu`` would be a no-password auth path in a
  production-shaped deployment (research-evidence.md correction 3, rules.md R-05);
* ``CPUExecutionProvider`` configured on the GPU tier would invalidate every latency number
  recorded that day (rules.md R-45).
"""

from __future__ import annotations

import functools
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DeploymentProfile(str, Enum):
    LOCAL_CPU = "local-cpu"
    AWS_GPU = "aws-gpu"


class ExecutionProvider(str, Enum):
    CPU = "CPUExecutionProvider"
    CUDA = "CUDAExecutionProvider"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="forbid",  # an unrecognized variable is a typo, and a silent typo is a wrong config
    )

    # -- tier ------------------------------------------------------------------------------------
    deployment_profile: DeploymentProfile
    execution_provider: ExecutionProvider

    # -- identity --------------------------------------------------------------------------------
    jwt_issuer: str
    jwt_jwks_url: str
    jwt_audience: str
    jwt_leeway_seconds: int = 30

    # -- edge ------------------------------------------------------------------------------------
    allowed_origins: str = Field(
        description="Comma-separated exact origins. No wildcards: an Origin allow-list with a "
        "wildcard is not an allow-list."
    )

    # -- downstream ------------------------------------------------------------------------------
    scorer_target: str
    scorer_deadline_ms: int = 400
    scorer_max_concurrency: int = 4
    database_url: SecretStr

    # -- secrets (same logical key names on both tiers; different providers) ----------------------
    hmac_key: SecretStr = Field(description="call_ref pseudonymization key")
    ticket_signing_key: SecretStr = Field(description="WSS stream-ticket signing key")
    audit_chain_key: SecretStr = Field(
        description="Audit hash-chain HMAC key. NEVER rotate once any audit event exists."
    )

    # -- policy ----------------------------------------------------------------------------------
    policy_bundle_path: Path = Path("/policy/policy.yaml")
    calibration_path: Path = Path("/policy/calibration.json")

    # -- retention / tenancy ---------------------------------------------------------------------
    audit_retention_days: int = 7
    tenant_id: str = "demo-tenant"  # decision D-7: forward-compat for Phase-4 RLS

    # -- operational -----------------------------------------------------------------------------
    max_concurrent_streams: int = 4  # refuse beyond this; never queue audio (rules.md R-20)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    git_commit: str = "unknown"

    # -- validators ------------------------------------------------------------------------------

    @field_validator("allowed_origins")
    @classmethod
    def _no_wildcard_origins(cls, value: str) -> str:
        origins = [o.strip() for o in value.split(",") if o.strip()]
        if not origins:
            raise ValueError("allowed_origins must list at least one exact origin")
        for origin in origins:
            if "*" in origin:
                raise ValueError("wildcard origins are not permitted")
            if not origin.startswith(("http://", "https://")):
                raise ValueError(f"origin must include a scheme: {origin!r}")
        return ",".join(origins)

    @field_validator("hmac_key", "ticket_signing_key", "audit_chain_key")
    @classmethod
    def _keys_long_enough(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("HMAC keys must be at least 32 characters")
        return value

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        aws = self.deployment_profile is DeploymentProfile.AWS_GPU

        if aws and self.execution_provider is not ExecutionProvider.CUDA:
            raise ValueError(
                "aws-gpu requires CUDAExecutionProvider: a CPU provider on the GPU tier "
                "invalidates every latency measurement (rules.md R-45)"
            )

        if aws and _is_test_issuer(self.jwt_issuer):
            raise ValueError(
                "the local test issuer must not be reachable under aws-gpu: it is a demo harness, "
                "not authentication (rules.md R-05)"
            )

        if aws and any(o.startswith("http://") for o in self.origin_list):
            raise ValueError("aws-gpu requires https origins")

        return self

    # -- derived ---------------------------------------------------------------------------------

    @property
    def origin_list(self) -> list[str]:
        return self.allowed_origins.split(",")

    @property
    def is_aws(self) -> bool:
        """For BANNERS, METRICS, and AUDIT ROWS only.

        Deliberately not used to select behaviour anywhere. If a branch on this appears in a request
        path, that is the R-04 violation the rule exists to catch.
        """
        return self.deployment_profile is DeploymentProfile.AWS_GPU


def _is_test_issuer(issuer: str) -> bool:
    lowered = issuer.lower()
    return "testidp" in lowered or "localhost" in lowered or "127.0.0.1" in lowered


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Cached so a missing variable fails once, loudly, at startup."""
    return Settings()  # values come from the environment
