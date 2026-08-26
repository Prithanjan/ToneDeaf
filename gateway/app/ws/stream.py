"""``/ws/v1/stream`` — the audio channel and the session lifecycle.

This is the only place in the system where raw audio exists, and it exists in exactly one object: the
per-session :class:`~app.audio.ring.VoicedRingBuffer`. It is never written to disk, never logged, never
placed in an error message, and it is cleared in a ``finally`` block that runs on close, error, and
disconnect alike (rules.md R-14).

Every other module this handler calls is pure or stateless. The handler's own job is sequencing:

    ticket -> origin -> session.open -> purpose match -> [frame -> VAD -> ring -> score -> policy
    -> audit -> emit] -> clear

Ordering choices that are controls rather than style:

* **Ticket and origin are checked before the socket is accepted.** An unauthenticated peer never
  reaches the frame parser.
* **``session.open`` must be the first message.** A binary frame arriving first is a protocol error,
  not audio to buffer, because there is no bound purpose to attach a decision to yet.
* **The audit row is written before the client is told the action.** If the write fails the client
  gets ``SCORER_UNAVAILABLE`` rather than an action with no evidence behind it — the product claim is
  persistent evidence, so an unrecorded decision is not a decision.
* **A failed score drops the window and keeps the stream.** It is not retried (a duplicate would enter
  the evidence sequence twice) and it is not counted as low risk (rules.md R-09).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.audio.ring import VoicedRingBuffer
from app.audio.vad import FrameVad, VadError
from app.constants import (
    MAX_TEXT_FRAME_BYTES,
    WS_SUBPROTOCOL,
)
from app.policy.diagnostics import DiagnosticsSidecar
from app.policy.engine import PolicyEngine, RiskState, WindowObservation
from app.scorer.client import ScorerUnavailable
from app.security.jwt import AuthError
from app.security.origin import OriginDenied, check_origin
from app.security.pseudonym import is_valid_call_ref
from app.security.ticket import TicketClaims, TicketError, extract_from_subprotocols, peek_binding, verify
from app.session_registry import SessionAlreadyStreaming, SessionError
from app.telemetry.logging import get_logger
from app.ws.frames import FrameRejected, check_sequence, parse_frame

router = APIRouter()
_log = get_logger(__name__)

# WebSocket close codes paired with app codes (technical-design.md section 2.5). Kept as one mapping so a new
# app code cannot be introduced without deciding its close code.
CLOSE_CODES: dict[str, int] = {
    "AUTH_TICKET_MISSING": 1008,
    "AUTH_TICKET_INVALID": 1008,
    "AUTH_ORIGIN_DENIED": 1008,
    "PROTO_FRAME_SIZE": 1003,
    "PROTO_SEQUENCE": 1003,
    "PROTO_FIRST_MESSAGE": 1003,
    "PROTO_PURPOSE_MISMATCH": 1008,
    "PROTO_PAYLOAD_TOO_LARGE": 1009,
    "SESSION_ALREADY_STREAMING": 1008,
    "BACKPRESSURE_REJECT": 1013,
    "SCORER_UNAVAILABLE": 1011,
}

#: Static close reasons. Never interpolate client input into one of these — a close reason is
#: recorded by proxies and browsers, and it is a documented path for a caller reference to escape
#: into a log (rules.md R-17).
CLOSE_REASONS: dict[str, str] = {
    "AUTH_TICKET_MISSING": "stream ticket not offered",
    "AUTH_TICKET_INVALID": "stream ticket not valid",
    "AUTH_ORIGIN_DENIED": "origin not permitted",
    "PROTO_FRAME_SIZE": "binary frame length invalid",
    "PROTO_SEQUENCE": "frame sequence invalid",
    "PROTO_FIRST_MESSAGE": "first message must be session.open",
    "PROTO_PURPOSE_MISMATCH": "purpose_code does not match the session",
    "PROTO_PAYLOAD_TOO_LARGE": "text frame too large",
    "SESSION_ALREADY_STREAMING": "session already streaming",
    "BACKPRESSURE_REJECT": "capacity reached",
    "SCORER_UNAVAILABLE": "scoring unavailable",
}


class ProtocolError(Exception):
    """A protocol violation with an app code from :data:`CLOSE_CODES`."""

    __slots__ = ("code",)

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(slots=True)
class _StreamCounters:
    frames_received: int = 0
    windows_scored: int = 0
    windows_dropped: int = 0


@router.websocket("/ws/v1/stream")
async def stream(websocket: WebSocket) -> None:
    state = websocket.app.state
    settings = state.settings

    # ---- handshake authorization, before accept() -----------------------------------------------
    try:
        offered = websocket.scope.get("subprotocols") or []
        raw_ticket = extract_from_subprotocols(offered)
        check_origin(websocket.headers.get("origin"), settings.origin_list)
    except (TicketError, OriginDenied) as exc:
        # Refuse the upgrade outright. There is no accepted socket to send a close frame on, which is
        # the correct outcome: an unauthenticated peer should never reach application code.
        await websocket.close(code=CLOSE_CODES.get(exc.code, 1008), reason=CLOSE_REASONS.get(exc.code, ""))
        return

    # The ticket carries the session and subject, so the session is resolved before the socket is
    # accepted and before any audio can arrive.
    try:
        claims = _verify_ticket(raw_ticket, state)
        record = state.registry.get(claims.session_id, owner_sub=claims.sub)
        state.replay_cache.spend(claims.jti, claims.exp)
    except (TicketError, SessionError, AuthError) as exc:
        code = getattr(exc, "code", "AUTH_TICKET_INVALID")
        await websocket.close(code=CLOSE_CODES.get(code, 1008), reason=CLOSE_REASONS.get(code, ""))
        return

    # ---- capacity: refuse, never queue (rules.md R-20) -------------------------------------------
    if state.live_streams >= settings.max_concurrent_streams:
        await websocket.close(
            code=CLOSE_CODES["BACKPRESSURE_REJECT"], reason=CLOSE_REASONS["BACKPRESSURE_REJECT"]
        )
        return

    try:
        state.registry.begin_stream(record)
    except SessionAlreadyStreaming as exc:
        await websocket.close(code=CLOSE_CODES[exc.code], reason=CLOSE_REASONS[exc.code])
        return

    await websocket.accept(subprotocol=WS_SUBPROTOCOL)
    state.live_streams += 1

    ring = VoicedRingBuffer()
    vad = FrameVad()
    counters = _StreamCounters()
    session_id = str(record.session_id)
    started = time.monotonic()

    try:
        await _run_session(websocket, state, record, ring, vad, counters)
    except ProtocolError as exc:
        await _close(websocket, exc.code)
    except WebSocketDisconnect:
        pass
    except ScorerUnavailable:
        await _close(websocket, "SCORER_UNAVAILABLE")
    except Exception:  # noqa: BLE001
        # exc_info goes through the redacting formatter, which scrubs bytes out of frame reprs.
        _log.error("stream failed", extra={"session_id": session_id}, exc_info=True)
        if websocket.client_state is WebSocketState.CONNECTED:
            await websocket.close(code=1011, reason="internal error")
    finally:
        # The one guarantee that matters most in this file (rules.md R-14). Runs on every exit path,
        # including an unhandled exception and a client that vanished mid-frame.
        ring.clear()
        state.live_streams = max(0, state.live_streams - 1)
        state.registry.end_stream(record)
        state.audit.forget(session_id)
        _log.info(
            "stream closed",
            extra={
                "session_id": session_id,
                "call_ref": record.call_ref,
                "frames_received": counters.frames_received,
                "windows_scored": counters.windows_scored,
                "duration_ms": int((time.monotonic() - started) * 1000),
            },
        )


def _verify_ticket(raw_ticket: str, state) -> TicketClaims:
    """Verify the ticket, binding it to the session and subject it claims.

    :func:`~app.security.ticket.peek_binding` returns an untrusted hint; :func:`verify` then re-reads
    the same claims from MAC-authenticated bytes, so a forged ``sid`` or ``sub`` fails the MAC check
    rather than selecting a different session.
    """
    session_id, sub = peek_binding(raw_ticket)
    return verify(
        state.settings.ticket_signing_key.get_secret_value().encode("utf-8"),
        raw_ticket,
        now=int(time.time()),
        expected_session_id=session_id,
        expected_sub=sub,
    )


async def _run_session(websocket, state, record, ring, vad, counters) -> None:
    settings = state.settings
    policy = state.policy
    diagnostics: DiagnosticsSidecar = state.diagnostics

    await _expect_session_open(websocket, record)

    engine = PolicyEngine(
        thresholds=policy.thresholds,
        purpose_code=record.purpose_code,
        purpose_actions=policy.purpose_actions,
    )
    scorer_health = state.scorer_health

    await _send(
        websocket,
        {
            "type": "session.accepted",
            "session_id": str(record.session_id),
            "policy_version": policy.version,
            "model_version": scorer_health.model_version,
            "calibration_version": policy.calibration.version,
            "deployment_profile": settings.deployment_profile.value,
            "execution_provider": scorer_health.execution_provider,
            "detector_mode": scorer_health.detector_mode,
            "artifact_state": policy.artifact_state,
        },
    )

    expected_seq = 0
    window_seq = 0

    while True:
        message = await websocket.receive()

        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000))

        text = message.get("text")
        if text is not None:
            # After session.open the channel is binary. A text frame here is either a client bug or
            # an attempt to re-declare purpose mid-stream; both are protocol errors.
            raise ProtocolError("PROTO_FIRST_MESSAGE")

        data = message.get("bytes")
        if data is None:
            raise ProtocolError("PROTO_FRAME_SIZE")

        # FrameRejected carries the sizes/sequence numbers as attributes rather than in its message,
        # so translating to a static close reason here loses nothing a log needs.
        try:
            frame = parse_frame(data)
            check_sequence(frame.seq, expected_seq)
        except FrameRejected as exc:
            raise ProtocolError(exc.code.value) from exc

        expected_seq = frame.seq + 1
        counters.frames_received += 1

        try:
            voiced = vad.is_voiced(frame.pcm)
        except VadError as exc:
            raise ProtocolError("PROTO_FRAME_SIZE") from exc

        pcm_window = ring.push(memoryview(frame.pcm).cast("h"), voiced=voiced)
        if pcm_window is None:
            continue

        try:
            score = await state.scorer.score_window(
                pcm_window=pcm_window,
                window_seq=window_seq,
                session_ref=record.call_ref,  # pseudonym only (rules.md R-16)
            )
        except ScorerUnavailable:
            # Drop the window; keep the stream. Not retried (a duplicate would enter the evidence
            # sequence twice) and NOT counted as low risk (rules.md R-09).
            counters.windows_dropped += 1
            window_seq += 1
            continue
        finally:
            # Release the 80 KiB copy before awaiting anything else. The ring buffer still holds the
            # samples; this is the transient serialization buffer.
            del pcm_window

        counters.windows_scored += 1
        record.windows_scored = counters.windows_scored

        # Advisory only. The return value is DISCARDED — that is decision D-12 as a code property
        # rather than a promise (rules.md R-12). Do not assign this to anything.
        diagnostics.observe(
            window_seq=window_seq, spoof_risk=score.spoof_risk, quality_flags=score.quality_flags
        )

        decision = engine.observe(
            WindowObservation(
                window_seq=window_seq,
                spoof_risk=score.spoof_risk,
                eligible=score.eligible,
                quality_flagged=bool(score.quality_flags),
            )
        )

        occurred_at = datetime.now(tz=timezone.utc)
        await _send(
            websocket,
            {
                "type": "risk.event",
                "window_seq": window_seq,
                "spoof_risk": round(score.spoof_risk, 4),
                "risk_state": decision.risk_state.value,
                "eligible": score.eligible,
                "quality_flags": list(score.quality_flags),
                "occurred_at": occurred_at.isoformat(),
            },
        )

        # An audit row on every scored window, not only on a state change: the evidence sequence is
        # the artifact, and a trail that records only the trigger cannot show why it fired.
        event_id, _seq = await state.audit.append(
            str(record.session_id),
            {
                "tenant_id": record.tenant_id,
                "session_id": record.session_id,
                "call_ref": record.call_ref,
                "occurred_at": occurred_at,
                "purpose_code": record.purpose_code,
                "context_value_band": record.context_value_band,
                "window_seq": window_seq,
                "spoof_risk": score.spoof_risk,
                "risk_state": decision.risk_state.value,
                "action": decision.action.value,
                "reason_code": decision.reason_code.value,
                "policy_version": policy.version,
                "policy_bundle_sha256": policy.sha256,
                "model_version": score.model_version,
                "model_sha256": scorer_health.model_sha256,
                "calibration_version": score.calibration_version,
                "calibration_sha256": scorer_health.calibration_sha256,
                "quality_flags": list(score.quality_flags),
                "detector_mode": score.detector_mode,
                "execution_provider": scorer_health.execution_provider,
                "deployment_profile": settings.deployment_profile.value,
            },
        )

        if decision.state_changed or decision.risk_state is RiskState.HIGH:
            await _send(
                websocket,
                {
                    "type": "policy.action",
                    "action": decision.action.value,
                    "risk_state": decision.risk_state.value,
                    "purpose_code": record.purpose_code,
                    "policy_version": policy.version,
                    "reason_code": decision.reason_code.value,
                    "audit_event_id": str(event_id),
                    "evidence_window_count": decision.eligible_window_count,
                    "evidence_high_count": decision.high_window_count,
                },
            )

        window_seq += 1


async def _expect_session_open(websocket, record) -> None:
    """Consume and validate the mandatory first message.

    The purpose check (decision D-4) is the substance here: ``purpose_code`` was bound server-side at
    ``POST /api/v1/sessions``, before any audio existed, and a mismatch means the client is trying to
    change the declared purpose on the audio channel.
    """
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))

    text = message.get("text")
    if text is None:
        raise ProtocolError("PROTO_FIRST_MESSAGE")
    if len(text.encode("utf-8")) > MAX_TEXT_FRAME_BYTES:
        raise ProtocolError("PROTO_PAYLOAD_TOO_LARGE")

    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise ProtocolError("PROTO_FIRST_MESSAGE") from exc

    if not isinstance(payload, dict) or payload.get("type") != "session.open":
        raise ProtocolError("PROTO_FIRST_MESSAGE")

    allowed = {"type", "call_ref", "purpose_code", "context_value_band", "client_capture"}
    if set(payload) - allowed:
        # No tolerated unknown fields: an ignored extra key is somewhere a client could put a
        # transcript or a phone number on the audio channel.
        raise ProtocolError("PROTO_FIRST_MESSAGE")

    call_ref = payload.get("call_ref")
    if not isinstance(call_ref, str) or not is_valid_call_ref(call_ref) or call_ref != record.call_ref:
        raise ProtocolError("PROTO_FIRST_MESSAGE")

    if payload.get("purpose_code") != record.purpose_code:
        raise ProtocolError("PROTO_PURPOSE_MISMATCH")
    if payload.get("context_value_band") != record.context_value_band:
        raise ProtocolError("PROTO_PURPOSE_MISMATCH")


async def _send(websocket, payload: dict[str, object]) -> None:
    await websocket.send_text(json.dumps(payload, separators=(",", ":")))


async def _close(websocket, code: str) -> None:
    if websocket.client_state is not WebSocketState.CONNECTED:
        return
    try:
        await websocket.send_text(
            # Fallback is "stream closed", not "rejected": the action vocabulary is banned from every
            # client-visible string (rules.md R-07), and this default is the one place a newly added
            # app code could smuggle one in before it gets a CLOSE_REASONS entry.
            json.dumps({"type": "error", "code": code, "message": CLOSE_REASONS.get(code, "stream closed")})
        )
    except Exception:  # noqa: BLE001 - the peer may already be gone; the close below still matters
        pass
    await websocket.close(code=CLOSE_CODES.get(code, 1011), reason=CLOSE_REASONS.get(code, ""))
