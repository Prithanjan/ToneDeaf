"""Gateway application factory.

The lifespan is where the fail-fast posture lives. Every check below refuses to start rather than
degrade, because each degradation it prevents is silent and each one falsifies something a judge will
be shown:

1. Settings load — a missing secret would otherwise become an HMAC over an empty key.
2. Policy bundle loads and hashes — no default threshold exists in code, so a missing bundle means the
   Gateway does not know what decision it is meant to make.
3. Scorer health — artifact identity is read and the execution provider is asserted. A silent CPU
   fallback on the GPU tier is a failure, not a degradation (rules.md R-45).
4. Startup banner prints the parity set, so the tier a terminal is looking at is unambiguous.

The banner is not decoration. Architecture.md section 5.1 defines parity as an identical *parity set*
rather than identical images (the images cannot be byte-identical — one links ``onnxruntime``, the
other ``onnxruntime-gpu``, rules.md R-06). Printing it at startup is what makes the Day-5 dual-tier
claim checkable in front of someone rather than asserted.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from hashlib import sha256
from pathlib import Path

import asyncpg
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.v1 import health as health_routes
from app.api.v1 import sessions as session_routes
from app.api.v1 import stream_ticket as ticket_routes
from app.audit.writer import AuditWriter
from app.config import Settings, get_settings
from app.constants import CHAIN_FIELD_SET_VERSION, CONTRACT_ID, WS_FRAME_BYTES
from app.policy.diagnostics import DiagnosticsSidecar
from app.policy.loader import PolicyBundle, PolicyLoadError, load_policy
from app.scorer.client import ScorerClient, ScorerHealth, ScorerUnavailable
from app.security.jwt import JwksCache, TokenValidator
from app.security.ticket import ReplayCache
from app.session_registry import SessionRegistry
from app.telemetry.logging import configure_logging, get_logger
from app.ws import stream as ws_routes

_log = get_logger(__name__)

#: Repo-root-relative paths used only to hash the contract files for the parity set. Absent in a slim
#: container image, in which case the hash reports as "unavailable" rather than failing startup — the
#: authoritative copy is the release manifest, and a missing file here is a packaging detail, not a
#: reason to refuse traffic.
_CONTRACT_PATHS = {
    "openapi": Path("contracts/openapi.yaml"),
    "proto": Path("contracts/voice_scorer.proto"),
}

SCORER_WAIT_SECONDS = 120


def _hash_contract(key: str) -> str:
    path = _CONTRACT_PATHS[key]
    try:
        return sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "unavailable"


async def _wait_for_scorer(client: ScorerClient, *, timeout_s: int) -> ScorerHealth:
    """Poll ``Health`` until the Scorer is ready.

    The Scorer loads an ONNX model, asserts its execution provider, and re-scores the fixed contract
    vector before it reports ready — on a cold GPU host that is tens of seconds. Waiting here rather
    than failing on the first refused connection is what makes ``docker compose up`` and an ECS
    rolling start behave the same way.
    """
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            health = await client.health()
            if health.ready:
                return health
        except ScorerUnavailable as exc:
            last_error = exc
        await asyncio.sleep(2.0)
    raise RuntimeError("scorer did not become ready within the startup window") from last_error


def _print_banner(settings: Settings, policy: PolicyBundle, health: ScorerHealth) -> None:
    lines = [
        "=" * 78,
        "SIH26104 Voice Integrity Control Plane — Gateway",
        "=" * 78,
        f"  deployment_profile     {settings.deployment_profile.value}",
        f"  execution_provider     {health.execution_provider}  (configured: {settings.execution_provider.value})",
        f"  git_commit             {settings.git_commit}",
        f"  frame_contract         {CONTRACT_ID}  ({WS_FRAME_BYTES}-byte frames)",
        f"  chain_field_set        {CHAIN_FIELD_SET_VERSION}",
        f"  policy_version         {policy.version}  sha256={policy.sha256[:16]}…",
        f"  threshold              high_window_risk={policy.thresholds.high_window_risk} "
        f"({policy.threshold_derivation})",
        f"  evidence_rule          {policy.thresholds.evidence_k}-of-{policy.thresholds.evidence_n} eligible windows",
        f"  model_version          {health.model_version}  sha256={health.model_sha256[:16]}…",
        f"  calibration_version    {policy.calibration.version}  sha256={policy.calibration.sha256[:16]}…",
        f"  detector_mode          {health.detector_mode}",
        f"  contract_vector_parity {health.contract_vector_parity_ok}",
        f"  artifact_state         {policy.artifact_state}",
        "-" * 78,
    ]

    if health.detector_mode != "REAL_DETECTOR":
        # Mock mode is loud, everywhere it could mislead (rules.md R-46).
        lines += [
            "  ** MOCK SMOKE MODE — THIS IS NOT A DETECTOR **",
            "  Scores are deterministic transport test data. Not evidence of detection.",
            "-" * 78,
        ]

    if policy.artifact_state != "policy_eligible":
        # rules.md R-11: no probability language until a calibration artifact says so.
        lines += [
            f"  ** ARTIFACT STATE {policy.artifact_state.upper()} — NOT POLICY ELIGIBLE **",
            "  spoof_risk must not be described as a probability in UI, logs, or docs.",
            "-" * 78,
        ]

    if not policy.allows_probability_language:
        lines.append("  calibration status is a placeholder; thresholds are provisional.")

    lines.append("=" * 78)
    for line in lines:
        _log.info(line, extra={"component": "banner"})


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        deployment_profile=settings.deployment_profile.value,
        git_commit=settings.git_commit,
    )

    try:
        policy = load_policy(settings.policy_bundle_path, settings.calibration_path)
    except PolicyLoadError:
        _log.error("policy bundle failed to load; refusing to start", exc_info=True)
        raise

    pool = await asyncpg.create_pool(
        settings.database_url.get_secret_value(),
        min_size=1,
        max_size=8,
        command_timeout=5.0,
    )

    scorer = ScorerClient(
        settings.scorer_target,
        deadline_ms=settings.scorer_deadline_ms,
        max_concurrency=settings.scorer_max_concurrency,
    )
    await scorer.start()
    scorer_health = await _wait_for_scorer(scorer, timeout_s=SCORER_WAIT_SECONDS)

    if scorer_health.execution_provider != settings.execution_provider.value:
        # Refuse to serve. Continuing would attach this tier's latency claims to the wrong provider,
        # which invalidates every measurement recorded that day (rules.md R-45).
        raise RuntimeError(
            "scorer execution provider does not match configuration: "
            f"{scorer_health.execution_provider!r} != {settings.execution_provider.value!r}"
        )

    if scorer_health.model_sha256 and policy.calibration.model_sha256 != scorer_health.model_sha256:
        # Catches the artifact-pairing mistake: thresholds calibrated for one model, applied to
        # another's score distribution.
        raise RuntimeError("calibration artifact model_sha256 does not match the loaded model")

    http_client = httpx.AsyncClient(timeout=3.0)

    app.state.settings = settings
    app.state.policy = policy
    app.state.pool = pool
    app.state.scorer = scorer
    app.state.scorer_health = scorer_health
    app.state.registry = SessionRegistry()
    app.state.replay_cache = ReplayCache(clock=lambda: int(time.time()))
    app.state.diagnostics = DiagnosticsSidecar(enabled=False)  # decision D-12
    app.state.audit = AuditWriter(
        pool, chain_key=settings.audit_chain_key.get_secret_value().encode("utf-8"),
        retention_days=settings.audit_retention_days,
    )
    app.state.token_validator = TokenValidator(
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
        jwks=JwksCache(settings.jwt_jwks_url, client=http_client),
        leeway_seconds=settings.jwt_leeway_seconds,
    )
    app.state.live_streams = 0
    app.state.api_schema_sha256 = _hash_contract("openapi")
    app.state.proto_sha256 = _hash_contract("proto")
    app.state.migration_head = await _read_migration_head(pool)

    _print_banner(settings, policy, scorer_health)

    try:
        yield
    finally:
        await scorer.close()
        await http_client.aclose()
        await pool.close()


async def _read_migration_head(pool: asyncpg.Pool) -> str:
    """Report the Alembic revision actually applied to this database.

    Part of the parity set: two tiers on the same commit but different migration heads would produce
    audit rows with different column sets, and the parity claim would be false in a way nobody looked
    for.
    """
    try:
        value = await pool.fetchval("SELECT version_num FROM alembic_version LIMIT 1")
        return str(value) if value else "none"
    except asyncpg.PostgresError:
        return "unavailable"


def create_app() -> FastAPI:
    app = FastAPI(
        title="Voice Integrity Control Plane API",
        version="1.0.0-phase1",
        lifespan=lifespan,
        # Docs are served; the OpenAPI document of record is contracts/openapi.yaml, and a CI check
        # compares the generated schema against it.
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    app.include_router(session_routes.router)
    app.include_router(ticket_routes.router)
    app.include_router(health_routes.router)
    app.include_router(ws_routes.router)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        """Static 500 body.

        A default handler that echoed the exception string would be a leak path: an exception raised
        while handling a caller reference can contain that reference in its message (rules.md R-17).
        """
        _log.error("unhandled error", extra={"component": "http"}, exc_info=True)
        return JSONResponse(
            status_code=500, content={"code": "INTERNAL", "message": "internal error"}
        )

    return app


app = create_app()
