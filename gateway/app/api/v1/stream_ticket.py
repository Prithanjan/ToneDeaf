"""``POST /api/v1/stream-ticket`` — mint a short-lived single-use WebSocket ticket.

Browsers cannot set an ``Authorization`` header on a WebSocket handshake. The two usual workarounds
both leak: a token in the query string lands in every access log, CloudFront log, and browser history
entry, and a cookie reintroduces CSRF surface on an endpoint that streams microphone audio.

So the bearer token is exchanged here, over a normal authenticated POST, for a ticket that is carried
in ``Sec-WebSocket-Protocol``. The ticket is signed, single-use, valid 60 seconds, and bound to both
``session_id`` and the caller's ``sub`` (decision D-6) — so it is useless to anyone who intercepts it,
against any other session, or a second time.
"""

from __future__ import annotations

import time
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from app.api.deps import CurrentPrincipal, CurrentRegistry, CurrentSettings
from app.constants import TICKET_TTL_SECONDS, WS_TICKET_SUBPROTOCOL_PREFIX
from app.security.ticket import TicketClaims, sign
from app.session_registry import SessionError

router = APIRouter(prefix="/api/v1", tags=["streaming"])


class StreamTicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str


class StreamTicketResponse(BaseModel):
    ticket: str
    subprotocol: str
    expires_in_seconds: int


@router.post("/stream-ticket", status_code=status.HTTP_201_CREATED, response_model=StreamTicketResponse)
async def create_stream_ticket(
    body: StreamTicketRequest,
    request: Request,
    principal: CurrentPrincipal,
    settings: CurrentSettings,
    registry: CurrentRegistry,
) -> StreamTicketResponse:
    try:
        record = registry.get(body.session_id, owner_sub=principal.sub)
    except SessionError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "SESSION_UNKNOWN", "message": "unknown session"},
        ) from exc

    if record.streaming:
        # Refuse rather than mint a ticket that would be rejected at the handshake. Failing here
        # gives the client an actionable status code instead of an opaque close frame.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "SESSION_ALREADY_STREAMING", "message": "session already has a live stream"},
        )

    claims = TicketClaims(
        session_id=str(record.session_id),
        sub=principal.sub,
        jti=uuid4().hex,
        exp=int(time.time()) + TICKET_TTL_SECONDS,
    )
    ticket = sign(settings.ticket_signing_key.get_secret_value().encode("utf-8"), claims)

    return StreamTicketResponse(
        ticket=ticket,
        # Pre-assembled so the client offers it verbatim. Assembling the subprotocol string
        # client-side is a place to get the prefix subtly wrong, and the failure would look like an
        # auth bug rather than a typo.
        subprotocol=f"{WS_TICKET_SUBPROTOCOL_PREFIX}{ticket}",
        expires_in_seconds=TICKET_TTL_SECONDS,
    )
