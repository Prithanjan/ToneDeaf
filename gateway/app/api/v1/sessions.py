"""``POST /api/v1/sessions`` — the only endpoint that ever sees a raw caller reference.

The whole privacy posture of the system hinges on what happens in the first four lines of the handler:
the raw ``client_call_ref`` is converted to an HMAC pseudonym and the raw value is then unreachable.
It is not logged, not stored, not echoed back, not forwarded to the Scorer, and there is no field on
:class:`~app.session_registry.SessionRecord` that could hold it (rules.md R-16).

``purpose_code`` and ``context_value_band`` are bound HERE, before any audio exists (decision D-4).
The WSS handshake later verifies that ``session.open`` echoes the same purpose. Accepting purpose on
the audio channel instead would let a client declare a low-risk purpose after hearing how the call was
going.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import CurrentPolicy, CurrentPrincipal, CurrentRegistry, CurrentSettings
from app.security.pseudonym import MAX_RAW_LENGTH, PseudonymError
from app.security.pseudonym import call_ref as compute_call_ref
from app.session_registry import SessionError
from app.telemetry.logging import get_logger

router = APIRouter(prefix="/api/v1", tags=["sessions"])
_log = get_logger(__name__)

PurposeCode = Literal[
    "payment_release", "beneficiary_change", "account_recovery", "support_enquiry"
]
ContextValueBand = Literal["low", "medium", "high", "unspecified"]


class CreateSessionRequest(BaseModel):
    """``extra="forbid"`` is a privacy control, not strictness for its own sake.

    A tolerated unknown field is a place a client could put a phone number or a transcript, and it
    would then appear in whatever generic request logging gets added later.
    """

    model_config = ConfigDict(extra="forbid")

    client_call_ref: Annotated[str, Field(min_length=1, max_length=MAX_RAW_LENGTH)]
    purpose_code: PurposeCode
    context_value_band: ContextValueBand
    consent_acknowledged: bool


class CreateSessionResponse(BaseModel):
    session_id: str
    call_ref: str
    purpose_code: str
    context_value_band: str
    policy_version: str
    retention_days: int
    expires_at: datetime
    artifact_state: str
    probability_language_permitted: bool


@router.post("/sessions", status_code=status.HTTP_201_CREATED, response_model=CreateSessionResponse)
async def create_session(
    body: CreateSessionRequest,
    principal: CurrentPrincipal,
    settings: CurrentSettings,
    policy: CurrentPolicy,
    registry: CurrentRegistry,
) -> CreateSessionResponse:
    if not body.consent_acknowledged:
        # The PWA makes getUserMedia structurally unreachable before acknowledgement
        # (rules.md R-18); this is the server-side half, so a non-PWA client cannot skip it.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "CONSENT_REQUIRED",
                "message": "consent must be acknowledged before capture",
            },
        )

    try:
        pseudonym = compute_call_ref(
            settings.hmac_key.get_secret_value().encode("utf-8"), body.client_call_ref
        )
    except PseudonymError as exc:
        # The message from PseudonymError never contains the offending value, which is why it can be
        # returned to the client at all.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_CALL_REF", "message": str(exc)},
        ) from exc

    # From this point the raw reference is out of scope and unreachable. Do not reintroduce it.
    try:
        record = registry.create(
            call_ref=pseudonym,
            purpose_code=body.purpose_code,
            context_value_band=body.context_value_band,
            owner_sub=principal.sub,
            tenant_id=settings.tenant_id,
            consent_acknowledged=True,
        )
    except SessionError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "CAPACITY", "message": "session capacity reached"},
        ) from exc

    _log.info(
        "session created",
        extra={
            "session_id": str(record.session_id),
            "call_ref": pseudonym,  # pseudonym, allow-listed in the logger
            "purpose_code": record.purpose_code,
            "policy_version": policy.version,
            "artifact_state": policy.artifact_state,
        },
    )

    return CreateSessionResponse(
        session_id=str(record.session_id),
        call_ref=pseudonym,
        purpose_code=record.purpose_code,
        context_value_band=record.context_value_band,
        policy_version=policy.version,
        retention_days=settings.audit_retention_days,
        expires_at=record.expires_at.astimezone(UTC),
        artifact_state=policy.artifact_state,
        # Returned so the PWA does not have to infer it. While this is false the UI must not use
        # probability language for spoof_risk (rules.md R-11).
        probability_language_permitted=policy.allows_probability_language,
    )
