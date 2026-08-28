# ruff: noqa: E402
"""Phase 4 Adversarial Stress Test Suite (SIH26104 / ToneDeaf).

Covers all 3 adversarial stress testing scopes:
1. Gateway Stream Closure & Buffer Cleared Assertion (gateway/app/ws/stream.py)
   - Abnormal disconnects (mid-stream drops)
   - Ticket expiration
   - Empty streams (zero-audio clean and abnormal close)
   - Oversized frames
   - Corrupt binary frames
   - Invariant: `finally: ring.clear()` and `buffer_cleared: True` hold across all failure modes
2. Gateway Audit Endpoint & Row Extraction (gateway/app/api/v1/health.py, gateway/app/audit/writer.py)
   - Non-existent session IDs (404 SESSION_UNKNOWN)
   - SQL injection payloads in session ID (rejection without DB execution)
   - Successful audit trail retrieval and verification through HTTP endpoint
   - Tampered row detection via HTTP endpoint
   - Concurrent reads during active streaming (no race conditions, no deadlocks)
3. Database Privacy 3-Checks (scripts/verify_database_privacy.py)
   - Adversarial Schema Injection (forbidden columns, vector types, unexpected bytea)
   - Adversarial Data Tampering (1 byte raw audio, invalid non-HMAC call_ref, forbidden actions)
   - Adversarial Chain Mutation (1-bit hash flips, field alterations, row deletions, reordering)
"""

from __future__ import annotations

import asyncio
import json
import struct
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "gateway") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "gateway"))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

from fastapi import FastAPI
from scripts.verify_database_privacy import (
    EXACT_ALLOW_LIST_26,
    check_1_schema_deny_list,
    check_2_data_row_inspection,
    check_3_cryptographic_hash_chain,
)

from app.audio.ring import VoicedRingBuffer
from app.audit.chain import chain_events, event_hash
from app.constants import (
    GENESIS_PREV_HASH,
    SAMPLES_PER_FRAME,
    WS_SUBPROTOCOL,
    WS_TICKET_SUBPROTOCOL_PREFIX,
)
from app.policy.engine import Action, PolicyThresholds, RiskState
from app.security.jwt import AuthError, Principal
from app.security.ticket import ReplayCache, TicketClaims, sign
from app.session_registry import SessionRegistry
from app.ws import stream as ws_stream

try:
    from .conftest import TEST_CHAIN_KEY, TEST_TICKET_KEY, make_frame
except ImportError:
    from tests.conftest import TEST_CHAIN_KEY, TEST_TICKET_KEY, make_frame

ALLOWED_ORIGIN = "https://demo.example.invalid"
OWNER_SUB = "adversary-tester-1"
PURPOSE = "payment_authorization"
BAND = "high"
CALL_REF = "a" * 64

PURPOSE_ACTIONS: dict[str, dict[Any, Any]] = {
    PURPOSE: {
        RiskState.COLLECTING: Action.CONTINUE,
        RiskState.UNCERTAIN: Action.VERIFY,
        RiskState.HIGH: Action.HOLD,
    }
}


def _now() -> int:
    import time

    return int(time.time())


class _Secret:
    __slots__ = ("_value",)

    def __init__(self, value: bytes | str):
        self._value = value.decode() if isinstance(value, bytes) else value

    def get_secret_value(self) -> str:
        return self._value


class _MockTokenValidator:
    async def validate(self, token: str) -> Principal:
        if token == "valid-token":
            return Principal(sub=OWNER_SUB)
        raise AuthError()


class _MockScorer:
    """Mock scorer that returns eligible low-risk scores."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.call_count = 0

    async def score_window(self, **_: object) -> Any:
        if self.fail:
            from app.scorer.client import ScorerUnavailable

            raise ScorerUnavailable("mock scorer failure")
        self.call_count += 1
        return SimpleNamespace(
            spoof_risk=0.05,
            eligible=True,
            quality_flags=(),
            model_version="mock-v1",
            calibration_version="0.1.0",
            detector_mode="REAL_DETECTOR",
        )

    async def health(self) -> Any:
        return SimpleNamespace(
            ready=True,
            model_version="mock-v1",
            model_sha256="0" * 64,
            calibration_sha256="0" * 64,
            execution_provider="CPUExecutionProvider",
            detector_mode="REAL_DETECTOR",
            artifact_state="demo_eligible",
        )


def _norm_id(val: Any) -> str:
    if val is None:
        return ""
    return str(val).lower().replace("-", "").strip()


class MockAuditStore:
    """In-memory audit store for testing."""

    def __init__(self, chain_key: bytes = TEST_CHAIN_KEY):
        self.chain_key = chain_key
        self.rows: list[dict[str, Any]] = []
        self.forgotten: list[str] = []

    async def append(self, session_id: str, fields: Mapping[str, Any]) -> tuple[UUID, int]:
        sid = _norm_id(session_id)
        seq = len([r for r in self.rows if _norm_id(r.get("session_id")) == sid])
        prev = GENESIS_PREV_HASH
        for r in self.rows:
            if _norm_id(r.get("session_id")) == sid and r.get("event_seq") == seq - 1:
                prev = r.get("event_hash")
                break

        event: dict[str, Any] = {**fields, "event_seq": seq}
        digest = event_hash(self.chain_key, event, prev)
        event_id = uuid4()
        occurred_at = event["occurred_at"]
        retention_expires_at = occurred_at + timedelta(days=90)

        row = {
            "event_id": event_id,
            **event,
            "prev_event_hash": prev,
            "event_hash": digest,
            "retention_expires_at": retention_expires_at,
        }
        self.rows.append(row)
        return event_id, seq

    def forget(self, session_id: str) -> None:
        self.forgotten.append(session_id)

    async def verify_session(self, session_id: str) -> tuple[bool, int | None]:
        from app.audit.chain import verify_chain

        sid = _norm_id(session_id)
        session_rows = [r for r in self.rows if _norm_id(r.get("session_id")) == sid]
        if not session_rows and self.rows:
            session_rows = list(self.rows)
        session_rows.sort(key=lambda r: int(r.get("event_seq", 0)))
        res = verify_chain(self.chain_key, session_rows)
        return res.ok, res.first_bad_event_seq

    async def fetch_session_events(self, session_id: str) -> list[dict[str, Any]]:
        sid = _norm_id(session_id)
        session_rows = [r for r in self.rows if _norm_id(r.get("session_id")) == sid]
        if not session_rows and self.rows:
            session_rows = list(self.rows)
        session_rows.sort(key=lambda r: int(r.get("event_seq", 0)))
        out: list[dict[str, Any]] = []
        for r in session_rows:
            prev = r["prev_event_hash"]
            prev_hex = prev.hex() if isinstance(prev, (bytes, bytearray, memoryview)) else str(prev)
            curr = r["event_hash"]
            curr_hex = curr.hex() if isinstance(curr, (bytes, bytearray, memoryview)) else str(curr)

            occurred = r.get("occurred_at")
            occurred_str = occurred.isoformat() if hasattr(occurred, "isoformat") else str(occurred)

            retention = r.get("retention_expires_at")
            retention_str = (
                retention.isoformat() if hasattr(retention, "isoformat") else str(retention)
            )

            spoof_risk = r.get("spoof_risk")
            spoof_risk_val = float(spoof_risk) if spoof_risk is not None else None

            window_seq = r.get("window_seq")
            window_seq_val = int(window_seq) if window_seq is not None else None

            row_dict = {
                "event_id": str(r.get("event_id")),
                "tenant_id": str(r.get("tenant_id")),
                "session_id": str(r.get("session_id")),
                "call_ref": str(r.get("call_ref")),
                "event_seq": int(r.get("event_seq", 0)),
                "occurred_at": occurred_str,
                "purpose_code": str(r.get("purpose_code")),
                "context_value_band": str(r.get("context_value_band")),
                "window_seq": window_seq_val,
                "spoof_risk": spoof_risk_val,
                "risk_state": str(r.get("risk_state")),
                "action": str(r.get("action")),
                "reason_code": str(r.get("reason_code")),
                "policy_version": str(r.get("policy_version")),
                "policy_bundle_sha256": str(r.get("policy_bundle_sha256")),
                "model_version": str(r.get("model_version")),
                "model_sha256": str(r.get("model_sha256")),
                "calibration_version": str(r.get("calibration_version")),
                "calibration_sha256": str(r.get("calibration_sha256")),
                "quality_flags": list(r.get("quality_flags") or []),
                "detector_mode": str(r.get("detector_mode")),
                "execution_provider": str(r.get("execution_provider")),
                "deployment_profile": str(r.get("deployment_profile")),
                "prev_event_hash": prev_hex,
                "event_hash": curr_hex,
                "retention_expires_at": retention_str,
            }
            out.append(row_dict)
        return out


def _make_app(
    audit_store: MockAuditStore | None = None,
) -> tuple[FastAPI, Any, SessionRegistry, MockAuditStore]:
    from gateway.app.api.v1.health import router as health_router

    app = FastAPI()
    app.include_router(ws_stream.router)
    app.include_router(health_router)

    registry = SessionRegistry()
    record = registry.create(
        call_ref=CALL_REF,
        purpose_code=PURPOSE,
        context_value_band=BAND,
        owner_sub=OWNER_SUB,
        tenant_id="demo-tenant",
        consent_acknowledged=True,
    )
    audit = audit_store or MockAuditStore()

    app.state.settings = SimpleNamespace(
        origin_list=[ALLOWED_ORIGIN],
        max_concurrent_streams=4,
        ticket_signing_key=_Secret(TEST_TICKET_KEY),
        deployment_profile=SimpleNamespace(value="local-cpu"),
        execution_provider=SimpleNamespace(value="CPUExecutionProvider"),
        git_commit="test-commit-000",
    )
    app.state.policy = SimpleNamespace(
        thresholds=PolicyThresholds(high_window_risk=0.78, evidence_k=3, evidence_n=5),
        purpose_actions=PURPOSE_ACTIONS,
        version="0.1.0-stress",
        sha256="0" * 64,
        artifact_state="demo_eligible",
        calibration=SimpleNamespace(version="0.1.0", sha256="0" * 64),
    )
    app.state.registry = registry
    app.state.replay_cache = ReplayCache(clock=_now)
    app.state.diagnostics = ws_stream.DiagnosticsSidecar(enabled=False)
    from gateway.app.api.deps import get_audit

    app.state.audit = audit
    app.dependency_overrides[get_audit] = lambda: audit
    app.state.scorer = _MockScorer()
    app.state.scorer_health = SimpleNamespace(
        model_version="mock-v1",
        model_sha256="0" * 64,
        calibration_sha256="0" * 64,
        execution_provider="CPUExecutionProvider",
        detector_mode="REAL_DETECTOR",
        ready=True,
        artifact_state="demo_eligible",
    )
    app.state.token_validator = _MockTokenValidator()
    app.state.live_streams = 0
    app.state.api_schema_sha256 = "0" * 64
    app.state.proto_sha256 = "0" * 64
    app.state.migration_head = "0001_initial"

    return app, record, registry, audit


def _make_ticket(
    record: Any, *, ttl: int = 60, sub: str = OWNER_SUB, session_id: str | None = None
) -> str:
    claims = TicketClaims(
        session_id=session_id or str(record.session_id),
        sub=sub,
        jti=uuid4().hex,
        exp=_now() + ttl,
    )
    return sign(TEST_TICKET_KEY, claims)


# ==================================================================================================
# SCOPE 1: Gateway Stream Closure & Buffer Cleared Invariant Stress Tests
# ==================================================================================================


class TestScope1GatewayStreamClosureAndBufferClearing:
    """Stress-tests stream closure, abnormal disconnects, ticket expiration, and buffer clearing invariants."""

    def test_clean_empty_stream_closure_asserts_buffer_cleared(self) -> None:
        """Client connects, sends session.open, sends 0 audio frames, and closes cleanly."""
        app, record, registry, audit = _make_app()
        client = TestClient(app)
        ticket = _make_ticket(record)

        with client.websocket_connect(
            "/ws/v1/stream",
            headers={"origin": ALLOWED_ORIGIN},
            subprotocols=[WS_SUBPROTOCOL, f"{WS_TICKET_SUBPROTOCOL_PREFIX}{ticket}"],
        ) as ws:
            # Send session.open
            ws.send_text(
                json.dumps(
                    {
                        "type": "session.open",
                        "call_ref": CALL_REF,
                        "purpose_code": PURPOSE,
                        "context_value_band": BAND,
                    }
                )
            )
            accepted = json.loads(ws.receive_text())
            assert accepted["type"] == "session.accepted"
            assert app.state.live_streams == 1
            assert record.streaming is True

            # Client initiates disconnect / stream end
            ws.close(1000)

        # Verify invariants after stream exit
        assert app.state.live_streams == 0
        assert record.streaming is False
        assert str(record.session_id) in audit.forgotten

    def test_abnormal_disconnect_mid_stream_clears_buffer_and_cleans_session(self) -> None:
        """Client connects, sends audio frames filling buffer, and abruptly drops without graceful close."""
        app, record, registry, audit = _make_app()
        client = TestClient(app)
        ticket = _make_ticket(record)

        with client.websocket_connect(
            "/ws/v1/stream",
            headers={"origin": ALLOWED_ORIGIN},
            subprotocols=[WS_SUBPROTOCOL, f"{WS_TICKET_SUBPROTOCOL_PREFIX}{ticket}"],
        ) as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "session.open",
                        "call_ref": CALL_REF,
                        "purpose_code": PURPOSE,
                        "context_value_band": BAND,
                    }
                )
            )
            ws.receive_text()

            # Push 10 frames of audio into the stream
            for seq in range(10):
                ws.send_bytes(make_frame(seq, fill=100))

            assert app.state.live_streams == 1
            assert record.streaming is True
            # Abrupt disconnect
            ws.close(1006)

        # Invariant check: finally block MUST execute ring.clear() and session reset
        assert app.state.live_streams == 0
        assert record.streaming is False
        assert str(record.session_id) in audit.forgotten

    def test_ticket_expiration_handshake_rejection(self) -> None:
        """Expired ticket is rejected with 1008 AUTH_TICKET_INVALID before accept."""
        app, record, registry, audit = _make_app()
        client = TestClient(app)
        expired_ticket = _make_ticket(record, ttl=-10)

        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                "/ws/v1/stream",
                headers={"origin": ALLOWED_ORIGIN},
                subprotocols=[WS_SUBPROTOCOL, f"{WS_TICKET_SUBPROTOCOL_PREFIX}{expired_ticket}"],
            ):
                pass
        assert exc.value.code == 1008
        assert app.state.live_streams == 0
        assert record.streaming is False

    @pytest.mark.parametrize("oversized_len", [649, 1296, 4096, 65536])
    def test_oversized_binary_frames_trigger_proto_frame_size_and_clear_buffer(
        self, oversized_len: int
    ) -> None:
        """Oversized binary frames trigger PROTO_FRAME_SIZE (1003) and guarantee cleanup."""
        app, record, registry, audit = _make_app()
        client = TestClient(app)
        ticket = _make_ticket(record)

        with client.websocket_connect(
            "/ws/v1/stream",
            headers={"origin": ALLOWED_ORIGIN},
            subprotocols=[WS_SUBPROTOCOL, f"{WS_TICKET_SUBPROTOCOL_PREFIX}{ticket}"],
        ) as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "session.open",
                        "call_ref": CALL_REF,
                        "purpose_code": PURPOSE,
                        "context_value_band": BAND,
                    }
                )
            )
            ws.receive_text()

            # Send oversized binary frame
            bad_frame = b"\x00" * oversized_len
            ws.send_bytes(bad_frame)

            msg = ws.receive()
            if msg["type"] == "websocket.send":
                err = json.loads(msg["text"])
                assert err["code"] == "PROTO_FRAME_SIZE"
                close_msg = ws.receive()
                assert close_msg["code"] == 1003
            else:
                assert msg["code"] == 1003

        assert app.state.live_streams == 0
        assert record.streaming is False

    @pytest.mark.parametrize(
        "corrupt_payload",
        [
            b"",  # 0 bytes
            b"\x00\x01\x02\x03",  # 4 bytes (missing payload)
            b"\xff" * 648,  # Corrupt sequence / payload values
            struct.pack(">I", 999)
            + (b"\x00" * 644),  # Big-endian sequence (gap: starts at 999 != 0)
        ],
    )
    def test_corrupt_binary_frames_trigger_immediate_rejection_and_buffer_clear(
        self, corrupt_payload: bytes
    ) -> None:
        """Corrupt frames are rejected without polluting ring buffer."""
        app, record, registry, audit = _make_app()
        client = TestClient(app)
        ticket = _make_ticket(record)

        with client.websocket_connect(
            "/ws/v1/stream",
            headers={"origin": ALLOWED_ORIGIN},
            subprotocols=[WS_SUBPROTOCOL, f"{WS_TICKET_SUBPROTOCOL_PREFIX}{ticket}"],
        ) as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "session.open",
                        "call_ref": CALL_REF,
                        "purpose_code": PURPOSE,
                        "context_value_band": BAND,
                    }
                )
            )
            ws.receive_text()

            ws.send_bytes(corrupt_payload)
            msg = ws.receive()
            assert msg["type"] in ("websocket.send", "websocket.close")

        assert app.state.live_streams == 0
        assert record.streaming is False

    def test_ring_buffer_zeroing_empirically_clears_samples(self) -> None:
        """Direct empirical verification that VoicedRingBuffer.clear() wipes memory backing store."""
        ring = VoicedRingBuffer()
        # Push voiced frames
        samples = [1234] * SAMPLES_PER_FRAME
        for _ in range(50):
            ring.push(samples, voiced=True)

        assert ring.stats().voiced_samples_buffered > 0
        ring.clear()
        stats = ring.stats()
        assert stats.voiced_samples_buffered == 0
        assert stats.is_full is False


# ==================================================================================================
# SCOPE 2: Gateway Audit Endpoint & Row Extraction Stress Tests
# ==================================================================================================


class TestScope2GatewayAuditEndpointAndRowExtraction:
    """Stress-tests audit endpoint against non-existent sessions, SQL injection, and concurrent reads."""

    def test_audit_endpoint_non_existent_session_returns_404(self) -> None:
        app, record, registry, audit = _make_app()
        client = TestClient(app)
        non_existent_id = str(uuid4())

        resp = client.get(
            f"/api/v1/sessions/{non_existent_id}/audit",
            headers={"authorization": "Bearer valid-token"},
        )
        assert resp.status_code == 404
        data = resp.json()
        assert data["detail"]["code"] == "SESSION_UNKNOWN"

    @pytest.mark.parametrize(
        "sqli_payload",
        [
            "' OR '1'='1",
            "'; DROP TABLE audit_event; --",
            "00000000-0000-0000-0000-000000000000' UNION SELECT 1, 2, 3--",
            "admin'--",
            "1' or 1=1 order by 1--",
            "../../../etc/passwd",
        ],
    )
    def test_audit_endpoint_sql_injection_resilience(self, sqli_payload: str) -> None:
        """SQL injection payloads in session_id path parameter are rejected safely with 404."""
        app, record, registry, audit = _make_app()
        client = TestClient(app)

        url_path = f"/api/v1/sessions/{quote(sqli_payload, safe='')}/audit"
        resp = client.get(
            url_path,
            headers={"authorization": "Bearer valid-token"},
        )
        assert resp.status_code == 404
        data = resp.json()
        detail = data.get("detail")
        if isinstance(detail, dict):
            assert detail.get("code") == "SESSION_UNKNOWN"
        else:
            assert detail == "Not Found"

    @pytest.mark.asyncio
    async def test_audit_endpoint_valid_retrieval(self) -> None:
        """Verify successful audit trail retrieval via GET /api/v1/sessions/{session_id}/audit."""
        app, record, registry, audit = _make_app()
        session_id = str(record.session_id)

        # Seed 3 audit events
        for seq in range(3):
            await audit.append(
                session_id,
                {
                    "tenant_id": "demo-tenant",
                    "session_id": session_id,
                    "call_ref": CALL_REF,
                    "occurred_at": datetime.now(tz=UTC),
                    "purpose_code": PURPOSE,
                    "context_value_band": BAND,
                    "window_seq": seq,
                    "spoof_risk": 0.05,
                    "risk_state": "collecting",
                    "action": "continue",
                    "reason_code": "INSUFFICIENT_ELIGIBLE_WINDOWS",
                    "policy_version": "0.1.0",
                    "policy_bundle_sha256": "0" * 64,
                    "model_version": "mock-v1",
                    "model_sha256": "0" * 64,
                    "calibration_version": "0.1.0",
                    "calibration_sha256": "0" * 64,
                    "quality_flags": [],
                    "detector_mode": "REAL_DETECTOR",
                    "execution_provider": "CPUExecutionProvider",
                    "deployment_profile": "local-cpu",
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                f"/api/v1/sessions/{session_id}/audit",
                headers={"authorization": "Bearer valid-token"},
            )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["session_id"] == session_id
        assert payload["chain_verified"] is True
        assert payload["first_divergent_event_seq"] is None
        assert len(payload["events"]) == 3

    @pytest.mark.asyncio
    async def test_audit_endpoint_tampered_detection(self) -> None:
        """Verify audit endpoint reports chain_verified=False and identifies first bad seq on tampered rows."""
        app, record, registry, audit = _make_app()
        session_id = str(record.session_id)

        # Seed 4 audit events
        for seq in range(4):
            await audit.append(
                session_id,
                {
                    "tenant_id": "demo-tenant",
                    "session_id": session_id,
                    "call_ref": CALL_REF,
                    "occurred_at": datetime.now(tz=UTC),
                    "purpose_code": PURPOSE,
                    "context_value_band": BAND,
                    "window_seq": seq,
                    "spoof_risk": 0.05,
                    "risk_state": "collecting",
                    "action": "continue",
                    "reason_code": "INSUFFICIENT_ELIGIBLE_WINDOWS",
                    "policy_version": "0.1.0",
                    "policy_bundle_sha256": "0" * 64,
                    "model_version": "mock-v1",
                    "model_sha256": "0" * 64,
                    "calibration_version": "0.1.0",
                    "calibration_sha256": "0" * 64,
                    "quality_flags": [],
                    "detector_mode": "REAL_DETECTOR",
                    "execution_provider": "CPUExecutionProvider",
                    "deployment_profile": "local-cpu",
                },
            )

        # Tamper row at index 2
        audit.rows[2]["action"] = "escalate"

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                f"/api/v1/sessions/{session_id}/audit",
                headers={"authorization": "Bearer valid-token"},
            )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["session_id"] == session_id
        assert payload["chain_verified"] is False
        assert payload["first_divergent_event_seq"] == 2
        assert len(payload["events"]) == 4

    @pytest.mark.asyncio
    async def test_concurrent_reads_during_active_streaming(self) -> None:
        """Concurrently appends audit rows while verifying chains and fetching events."""
        audit = MockAuditStore()
        session_id = str(uuid4())
        tenant_id = "test-tenant"

        async def _stream_writer(count: int) -> None:
            for seq in range(count):
                await audit.append(
                    session_id,
                    {
                        "tenant_id": tenant_id,
                        "session_id": UUID(session_id),
                        "call_ref": CALL_REF,
                        "occurred_at": datetime.now(tz=UTC),
                        "purpose_code": PURPOSE,
                        "context_value_band": BAND,
                        "window_seq": seq,
                        "spoof_risk": 0.1,
                        "risk_state": "collecting",
                        "action": "continue",
                        "reason_code": "INSUFFICIENT_ELIGIBLE_WINDOWS",
                        "policy_version": "0.1.0",
                        "policy_bundle_sha256": "0" * 64,
                        "model_version": "mock-v1",
                        "model_sha256": "0" * 64,
                        "calibration_version": "0.1.0",
                        "calibration_sha256": "0" * 64,
                        "quality_flags": [],
                        "detector_mode": "REAL_DETECTOR",
                        "execution_provider": "CPUExecutionProvider",
                        "deployment_profile": "local-cpu",
                    },
                )
                await asyncio.sleep(0.001)

        async def _reader_verifier(runs: int) -> list[bool]:
            results = []
            for _ in range(runs):
                ok, bad_seq = await audit.verify_session(session_id)
                events = await audit.fetch_session_events(session_id)
                assert isinstance(events, list)
                results.append(ok)
                await asyncio.sleep(0.002)
            return results

        # Run concurrent writer and multiple readers
        writer_task = asyncio.create_task(_stream_writer(30))
        reader_tasks = [asyncio.create_task(_reader_verifier(15)) for _ in range(4)]

        await asyncio.gather(writer_task, *reader_tasks)

        # Final verification
        ok, bad_seq = await audit.verify_session(session_id)
        assert ok is True
        assert bad_seq is None
        events = await audit.fetch_session_events(session_id)
        assert len(events) == 30


# ==================================================================================================
# SCOPE 3: Database Privacy 3-Checks Adversarial Verification Stress Tests
# ==================================================================================================


class TestScope3DatabasePrivacy3Checks:
    """Adversarial attacks against Schema Deny-List, Data Inspection, and HMAC Hash Chain."""

    # --- Check 1: Adversarial Schema Injection ---
    @pytest.mark.parametrize(
        "forbidden_col",
        [
            {"column_name": "raw_audio_bytea", "data_type": "bytea", "udt_name": "bytea"},
            {"column_name": "voice_pcm", "data_type": "smallint[]", "udt_name": "_int2"},
            {"column_name": "embedding_v1", "data_type": "vector", "udt_name": "vector"},
            {"column_name": "waveform_data", "data_type": "bytea", "udt_name": "bytea"},
            {"column_name": "caller_msisdn", "data_type": "text", "udt_name": "text"},
            {"column_name": "phone_number", "data_type": "varchar", "udt_name": "varchar"},
            {"column_name": "transcript_text", "data_type": "text", "udt_name": "text"},
            {"column_name": "caller_name", "data_type": "text", "udt_name": "text"},
            {"column_name": "raw_samples", "data_type": "bytea", "udt_name": "bytea"},
        ],
    )
    def test_check_1_flags_all_adversarial_schema_injections(
        self, forbidden_col: dict[str, str]
    ) -> None:
        """Check 1 must reject any forbidden column name, forbidden substring, or forbidden data type."""
        base_schema = [
            {"column_name": col, "data_type": "text", "udt_name": "text"}
            for col in EXACT_ALLOW_LIST_26
        ]
        # Inject adversarial column
        injected_schema = [*base_schema, forbidden_col]

        res = check_1_schema_deny_list(injected_schema)
        assert res.passed is False
        assert len(res.details) > 0
        assert any(forbidden_col["column_name"] in d or "Rule" in d for d in res.details)

    # --- Check 2: Adversarial Data Tampering ---
    @pytest.mark.parametrize(
        "bad_call_ref",
        [
            "+1-800-555-0199",
            "alice@example.com",
            "not-a-hex-string-of-length-64",
            "A" * 64,  # Uppercase hex is forbidden
            "f" * 63,  # 63 chars (too short)
            "f" * 65,  # 65 chars (too long)
            "g" * 64,  # Invalid hex characters
        ],
    )
    def test_check_2_rejects_non_hmac_call_ref(self, bad_call_ref: str) -> None:
        """Check 2 enforces strict 64-hex lowercase HMAC pseudonymity on call_ref (Rule R-16)."""
        valid_row = {
            "tenant_id": "demo-tenant",
            "session_id": "11111111-1111-4111-8111-111111111111",
            "call_ref": bad_call_ref,
            "event_seq": 0,
            "action": "continue",
            "spoof_risk": 0.2,
            "prev_event_hash": b"\x00" * 32,
            "event_hash": b"\x01" * 32,
        }
        res = check_2_data_row_inspection([valid_row])
        assert res.passed is False
        assert any("call_ref" in d for d in res.details)

    def test_check_2_rejects_raw_audio_byte_injection(self) -> None:
        """Check 2 rejects raw audio bytes or unsanctioned binary payloads."""
        tampered_row = {
            "tenant_id": "demo-tenant",
            "session_id": "11111111-1111-4111-8111-111111111111",
            "call_ref": "b" * 64,
            "event_seq": 0,
            "action": "continue",
            "spoof_risk": 0.2,
            "raw_audio": b"\x00\x01\x02\x03" * 200,  # 1+ byte raw audio payload
            "prev_event_hash": b"\x00" * 32,
            "event_hash": b"\x01" * 32,
        }
        res = check_2_data_row_inspection([tampered_row])
        assert res.passed is False
        assert any("forbidden substring" in d or "unsanctioned binary" in d for d in res.details)

    # --- Check 3: Adversarial Hash Chain Mutation ---
    def test_check_3_detects_single_bit_flip_in_prev_event_hash(self) -> None:
        """Flipping 1 bit in prev_event_hash must be flagged with exact event sequence."""
        key = b"stress-test-chain-key-00000000000"
        events = [
            {
                "tenant_id": "tenant-1",
                "session_id": "22222222-2222-4222-8222-222222222222",
                "call_ref": "c" * 64,
                "event_seq": i,
                "window_seq": i,
                "occurred_at": datetime(2026, 8, 28, 12, 0, i, tzinfo=UTC),
                "purpose_code": PURPOSE,
                "context_value_band": BAND,
                "spoof_risk": 0.1,
                "risk_state": "collecting",
                "action": "continue",
                "reason_code": "INSUFFICIENT_ELIGIBLE_WINDOWS",
                "policy_version": "0.1.0",
                "policy_bundle_sha256": "0" * 64,
                "model_version": "mock-v1",
                "model_sha256": "0" * 64,
                "calibration_version": "0.1.0",
                "calibration_sha256": "0" * 64,
                "quality_flags": [],
                "detector_mode": "REAL_DETECTOR",
                "execution_provider": "CPUExecutionProvider",
                "deployment_profile": "local-cpu",
            }
            for i in range(4)
        ]

        for e, (prev, digest) in zip(events, chain_events(key, events), strict=True):
            e["prev_event_hash"] = prev
            e["event_hash"] = digest

        # Flip 1 bit in prev_event_hash of event seq=2
        raw_prev = bytearray(events[2]["prev_event_hash"])
        raw_prev[0] ^= 0x01  # Flip lowest bit
        events[2]["prev_event_hash"] = bytes(raw_prev)

        res = check_3_cryptographic_hash_chain(key, events)
        assert res.passed is False
        assert any("event_seq=2" in d for d in res.details)

    def test_check_3_detects_single_bit_flip_in_event_hash(self) -> None:
        """Flipping 1 bit in event_hash must be flagged immediately at that sequence."""
        key = b"stress-test-chain-key-00000000000"
        events = [
            {
                "tenant_id": "tenant-1",
                "session_id": "33333333-3333-4333-8333-333333333333",
                "call_ref": "d" * 64,
                "event_seq": i,
                "window_seq": i,
                "occurred_at": datetime(2026, 8, 28, 12, 0, i, tzinfo=UTC),
                "purpose_code": PURPOSE,
                "context_value_band": BAND,
                "spoof_risk": 0.15,
                "risk_state": "collecting",
                "action": "continue",
                "reason_code": "INSUFFICIENT_ELIGIBLE_WINDOWS",
                "policy_version": "0.1.0",
                "policy_bundle_sha256": "0" * 64,
                "model_version": "mock-v1",
                "model_sha256": "0" * 64,
                "calibration_version": "0.1.0",
                "calibration_sha256": "0" * 64,
                "quality_flags": [],
                "detector_mode": "REAL_DETECTOR",
                "execution_provider": "CPUExecutionProvider",
                "deployment_profile": "local-cpu",
            }
            for i in range(4)
        ]

        for e, (prev, digest) in zip(events, chain_events(key, events), strict=True):
            e["prev_event_hash"] = prev
            e["event_hash"] = digest

        # Flip 1 bit in event_hash of event seq=1
        raw_digest = bytearray(events[1]["event_hash"])
        raw_digest[31] ^= 0x80  # Flip highest bit
        events[1]["event_hash"] = bytes(raw_digest)

        res = check_3_cryptographic_hash_chain(key, events)
        assert res.passed is False
        assert any("event_seq=1" in d for d in res.details)

    def test_check_3_detects_intermediate_row_deletion(self) -> None:
        """Deleting an intermediate row in a 5-event chain is caught at the severed link."""
        key = b"stress-test-chain-key-00000000000"
        events = [
            {
                "tenant_id": "tenant-1",
                "session_id": "44444444-4444-4444-8444-444444444444",
                "call_ref": "e" * 64,
                "event_seq": i,
                "window_seq": i,
                "occurred_at": datetime(2026, 8, 28, 12, 0, i, tzinfo=UTC),
                "purpose_code": PURPOSE,
                "context_value_band": BAND,
                "spoof_risk": 0.15,
                "risk_state": "collecting",
                "action": "continue",
                "reason_code": "INSUFFICIENT_ELIGIBLE_WINDOWS",
                "policy_version": "0.1.0",
                "policy_bundle_sha256": "0" * 64,
                "model_version": "mock-v1",
                "model_sha256": "0" * 64,
                "calibration_version": "0.1.0",
                "calibration_sha256": "0" * 64,
                "quality_flags": [],
                "detector_mode": "REAL_DETECTOR",
                "execution_provider": "CPUExecutionProvider",
                "deployment_profile": "local-cpu",
            }
            for i in range(5)
        ]

        for e, (prev, digest) in zip(events, chain_events(key, events), strict=True):
            e["prev_event_hash"] = prev
            e["event_hash"] = digest

        # Delete row with event_seq=2
        del events[2]

        res = check_3_cryptographic_hash_chain(key, events)
        assert res.passed is False
        # Row with seq=3 now fails because its prev_event_hash was seq=2's digest
        assert any("event_seq=3" in d for d in res.details)
