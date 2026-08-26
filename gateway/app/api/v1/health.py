"""Health, readiness, version, and the session audit read.

``/api/v1/version`` returns the **parity set** (architecture.md section 5.1). It is how a judge
verifies that the AWS tier and the local CPU tier are running the same signed release: the hashes must
match across tiers even though the images cannot be byte-identical, because one links
``onnxruntime`` and the other ``onnxruntime-gpu`` (rules.md R-06). Claiming identical images would be
false; claiming an identical parity set is both true and checkable, and this endpoint is what makes it
checkable rather than asserted.

Liveness and readiness are separate on purpose. ``/healthz`` answers "is the process running", so a
Scorer outage does not make the ALB kill an otherwise healthy Gateway. ``/readyz`` answers "can this
instance serve a stream", which is what a deploy gate should wait on.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel

from app.api.deps import CurrentPolicy, CurrentPrincipal, CurrentRegistry, CurrentSettings
from app.scorer.client import ScorerUnavailable
from app.session_registry import SessionError

router = APIRouter(tags=["ops"])


class ReadinessReport(BaseModel):
    ready: bool
    database: bool
    scorer: bool
    policy_bundle: bool
    secrets: bool


class VersionInfo(BaseModel):
    git_commit: str
    deployment_profile: str
    execution_provider: str
    api_schema_sha256: str
    proto_sha256: str
    policy_version: str
    policy_bundle_sha256: str
    model_version: str
    model_sha256: str
    calibration_version: str
    calibration_sha256: str
    migration_head: str
    detector_mode: str
    artifact_state: str


@router.get("/healthz", include_in_schema=False)
async def healthz() -> Response:
    return Response(status_code=status.HTTP_200_OK, content=b"ok", media_type="text/plain")


@router.get("/readyz", response_model=ReadinessReport)
async def readyz(request: Request, response: Response, settings: CurrentSettings) -> ReadinessReport:
    state = request.app.state

    database = False
    try:
        await state.pool.fetchval("SELECT 1")
        database = True
    except Exception:  # noqa: BLE001 - any failure means not ready; the reason is already logged
        database = False

    scorer_ready = False
    try:
        health = await state.scorer.health()
        # The provider check is part of readiness, not a warning. A silent CPU fallback on the GPU
        # tier is a failure, not a degradation (rules.md R-45) — serving traffic from it would attach
        # GPU-tier latency claims to CPU-tier measurements.
        scorer_ready = health.ready and health.execution_provider == settings.execution_provider.value
    except ScorerUnavailable:
        scorer_ready = False

    report = ReadinessReport(
        ready=database and scorer_ready,
        database=database,
        scorer=scorer_ready,
        policy_bundle=state.policy is not None,
        secrets=True,  # config.Settings refuses to start without them, so reaching here proves it
    )
    if not report.ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return report


@router.get("/api/v1/version", response_model=VersionInfo)
async def version(request: Request, settings: CurrentSettings, policy: CurrentPolicy) -> VersionInfo:
    state = request.app.state

    # Read from the Scorer rather than from local config: the point of this endpoint is to report
    # what is actually loaded. Config would report what was intended, which is the thing being
    # verified.
    model_version = ""
    model_sha256 = ""
    calibration_sha256 = policy.calibration.sha256
    detector_mode = "UNKNOWN"
    execution_provider = "UNKNOWN"
    artifact_state = policy.artifact_state
    try:
        health = await state.scorer.health()
        model_version = health.model_version
        model_sha256 = health.model_sha256
        calibration_sha256 = health.calibration_sha256
        detector_mode = health.detector_mode
        execution_provider = health.execution_provider
        # The weaker of the two wins. A policy bundle cannot promote a mock detector, and a real
        # detector cannot promote a placeholder threshold.
        if health.artifact_state == "research_only" or artifact_state == "research_only":
            artifact_state = "research_only"
        elif "demo_eligible" in (health.artifact_state, artifact_state):
            artifact_state = "demo_eligible"
    except ScorerUnavailable:
        artifact_state = "research_only"

    return VersionInfo(
        git_commit=settings.git_commit,
        deployment_profile=settings.deployment_profile.value,
        execution_provider=execution_provider,
        api_schema_sha256=state.api_schema_sha256,
        proto_sha256=state.proto_sha256,
        policy_version=policy.version,
        policy_bundle_sha256=policy.sha256,
        model_version=model_version,
        model_sha256=model_sha256,
        calibration_version=policy.calibration.version,
        calibration_sha256=calibration_sha256,
        migration_head=state.migration_head,
        detector_mode=detector_mode,
        artifact_state=artifact_state,
    )


@router.get("/api/v1/sessions/{session_id}/audit", tags=["audit"])
async def session_audit(
    session_id: str,
    request: Request,
    principal: CurrentPrincipal,
    registry: CurrentRegistry,
) -> dict[str, object]:
    """Feature-only audit trail. Phase 4 renders this in the Privacy Inspector.

    There is no parameter, no field, and no code path here that can return audio. The response shape
    is the allow-listed audit columns and nothing else (technical-design.md section 5.1).
    """
    try:
        registry.get(session_id, owner_sub=principal.sub)
    except SessionError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "SESSION_UNKNOWN", "message": "unknown session"},
        ) from exc

    ok, first_bad_seq = await request.app.state.audit.verify_session(session_id)
    return {
        "session_id": session_id,
        "chain_verified": ok,
        "first_divergent_event_seq": first_bad_seq,
        # Phase 4 fills this from the allow-listed columns. Returning an empty list in Phase 1 is
        # honest; returning a plausible-looking stub would not be (rules.md R-01).
        "events": [],
        "phase_note": "event listing lands in Phase 4; chain verification is live now",
    }
