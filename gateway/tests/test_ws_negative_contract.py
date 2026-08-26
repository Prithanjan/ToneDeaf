"""WSS negative-contract suite — the Phase 1 exit criteria.

`Part-2 (Claude Scoped)` names four of these as **Phase 1 exit criteria**: missing ticket, wrong
``Origin``, duplicate sequence, wrong byte length. phases.md §2.4 keeps two more that the plan dropped
(purpose mismatch, oversized text frame) because both are reachable from a browser and both fail in a
way that looks like a network fault rather than a rejection.

What this file tests that no other suite can
--------------------------------------------
The pure suites already cover the *decisions*: ``test_frames.py`` proves a 647-byte frame is refused and
a duplicate sequence is the same error as a gap; ``test_ticket.py`` proves a missing subprotocol yields
``AUTH_TICKET_MISSING``. None of them prove the **wiring** — that the handler turns each rejection into
the right close code *and* the right app code, at the right point in the sequence. That gap is the whole
reason this file exists: a correct ``parse_frame`` behind a handler that closes 1011 "internal error"
fails the exit criterion while every unit test stays green.

Note that the WebSocket close code alone is too coarse to be the assertion. Three different app codes
share 1003, so a handler that confused ``PROTO_FRAME_SIZE`` with ``PROTO_SEQUENCE`` would satisfy a
close-code-only test. Every case here asserts the app code from the error frame as well.

Two orderings are controls rather than style, and the shape of the assertion encodes both:

* **Authorization completes before ``accept()``.** :func:`_expect_refused` can only pass if the first
  ASGI message the handler sent was ``websocket.close`` and not ``websocket.accept`` — so an
  unauthenticated peer provably never reached the frame parser. Moving any of those checks after
  ``accept()`` turns those tests red rather than leaving them silently green.
* **Ticket extraction precedes the origin check.** They share a ``try``, so the precedence is real and
  observable: a request with neither gets ``AUTH_TICKET_MISSING``. Pinned so a reorder is deliberate.

⚠️ Why this module skips instead of failing to import
-----------------------------------------------------
Importing ``app.ws.stream`` pulls in the whole serving stack — ``fastapi``, ``webrtcvad``, ``grpcio``
plus the **generated** protobuf stubs, ``httpx``, ``python-jose``. On the development workstation none of
that installs: the pinned interpreter is 3.12 (memory.md D-10) but the local one is 3.14, and
``pydantic-core`` has no 3.14 wheel for this platform. A bare import would turn one environment problem
into a collection error that reddens all 239 otherwise-passing tests, so the module skips.

**A skip is not a pass, and CI must treat it as a failure.** These are exit criteria; a gate wired to a
suite that silently skipped is worse than no gate, because it reports green. The `contract` job must
assert this suite actually *collected and ran*. There is also an ordering dependency:
``scripts/gen_proto.sh`` has to run **before** pytest, or the generated stubs are absent and this module
skips inside CI for the same reason it skips locally.

The Scorer, the audit writer, and the policy bundle are faked. The handler's real collaborators — the
registry, the replay cache, the frame parser, the VAD, the ring buffer, the policy engine — all run for
real, because they are the parts these criteria are about.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.constants import (
    MAX_TEXT_FRAME_BYTES,
    WS_FRAME_BYTES,
    WS_SUBPROTOCOL,
    WS_TICKET_SUBPROTOCOL_PREFIX,
)
from app.policy.engine import Action, PolicyThresholds, RiskState
from app.security.ticket import ReplayCache, TicketClaims, sign
from app.session_registry import SessionRegistry
from tests.conftest import TEST_TICKET_KEY, make_frame

pytestmark = pytest.mark.contract

# One guard for the whole serving stack rather than one per dependency: the reason a developer needs is
# "you are not on the pinned interpreter", not a list of six module names.
ws_stream = pytest.importorskip(
    "app.ws.stream",
    reason=(
        "needs the full serving stack (fastapi, webrtcvad, grpcio + generated stubs, httpx, jose). "
        "Run on Python 3.12 after scripts/gen_proto.sh. A skip here in CI is a FAILURE: these are "
        "Phase 1 exit criteria."
    ),
)
fastapi = pytest.importorskip("fastapi")
starlette_ws = pytest.importorskip("starlette.websockets")
testclient = pytest.importorskip("starlette.testclient")

WebSocketDisconnect = starlette_ws.WebSocketDisconnect
TestClient = testclient.TestClient

ALLOWED_ORIGIN = "https://demo.example.invalid"
DENIED_ORIGIN = "https://attacker.example.invalid"
OWNER_SUB = "analyst-1"
PURPOSE = "payment_authorization"
BAND = "high"
CALL_REF = "b" * 64  # a well-formed pseudonym: 64 lowercase hex

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
    """Minimal stand-in for pydantic ``SecretStr``.

    Hand-rolled because pydantic is the one dependency that cannot be built on the development
    workstation at all, and a fake settings object should not be the reason this file needs it.
    """

    __slots__ = ("_value",)

    def __init__(self, value: bytes | str):
        self._value = value.decode() if isinstance(value, bytes) else value

    def get_secret_value(self) -> str:
        return self._value


class _FakeScorer:
    """Never reached by any case in this file, and that is the assertion.

    Every case here is refused before a full 2.56 s window can accumulate, so a call to
    ``score_window`` means the handler let malformed input through to inference. Raising rather than
    returning a plausible score turns that into a loud failure instead of a passing test.
    """

    async def score_window(self, **_: object) -> object:  # pragma: no cover - must not be called
        raise AssertionError("a negative-contract case reached the Scorer")


class _FakeAudit:
    """Records what it was asked to write, so tests can assert it was asked for nothing.

    An audit row written during a rejected handshake would mean the evidence trail contains events for
    a stream that never carried audio.
    """

    def __init__(self) -> None:
        self.appended: list[dict[str, object]] = []
        self.forgotten: list[str] = []

    async def append(self, session_id: str, event: dict[str, object]) -> tuple[UUID, int]:
        self.appended.append(event)
        return uuid4(), len(self.appended)

    def forget(self, session_id: str) -> None:
        self.forgotten.append(session_id)


def _policy() -> SimpleNamespace:
    return SimpleNamespace(
        thresholds=PolicyThresholds(high_window_risk=0.78, evidence_k=3, evidence_n=5),
        purpose_actions=PURPOSE_ACTIONS,
        version="phase1-placeholder",
        sha256="0" * 64,
        artifact_state="placeholder-not-policy-eligible",
        calibration=SimpleNamespace(version="none", sha256="0" * 64),
    )


def _settings(*, max_streams: int = 4) -> SimpleNamespace:
    return SimpleNamespace(
        origin_list=[ALLOWED_ORIGIN],
        max_concurrent_streams=max_streams,
        ticket_signing_key=_Secret(TEST_TICKET_KEY),
        deployment_profile=SimpleNamespace(value="local-cpu"),
    )


@pytest.fixture
def harness() -> Any:
    """A FastAPI app carrying only the WS router, with a hand-built ``app.state``.

    ``main.create_app()`` is deliberately bypassed: its lifespan opens a Postgres pool and blocks for
    up to 120 s waiting on a live Scorer. Neither is needed to prove a malformed frame is refused, and
    depending on them would turn the exit-criteria suite into an integration test that gets skipped —
    which is the failure mode this file exists to avoid.
    """
    app = fastapi.FastAPI()
    app.include_router(ws_stream.router)

    registry = SessionRegistry()
    record = registry.create(
        call_ref=CALL_REF,
        purpose_code=PURPOSE,
        context_value_band=BAND,
        owner_sub=OWNER_SUB,
        tenant_id="demo-tenant",
        consent_acknowledged=True,
    )
    audit = _FakeAudit()

    app.state.settings = _settings()
    app.state.policy = _policy()
    app.state.registry = registry
    app.state.replay_cache = ReplayCache(clock=_now)
    app.state.diagnostics = ws_stream.DiagnosticsSidecar(enabled=False)
    app.state.audit = audit
    app.state.scorer = _FakeScorer()
    app.state.scorer_health = SimpleNamespace(
        model_version="mock",
        model_sha256="0" * 64,
        calibration_sha256="0" * 64,
        execution_provider="CPUExecutionProvider",
        detector_mode="MOCK_SMOKE",
    )
    app.state.live_streams = 0

    return SimpleNamespace(app=app, record=record, registry=registry, audit=audit)


# ---------------------------------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------------------------------
def _ticket(
    harness: Any,
    *,
    session_id: str | None = None,
    sub: str = OWNER_SUB,
    ttl: int = 60,
    jti: str | None = None,
) -> str:
    claims = TicketClaims(
        session_id=session_id or str(harness.record.session_id),
        sub=sub,
        jti=jti or uuid4().hex,
        exp=_now() + ttl,
    )
    return sign(TEST_TICKET_KEY, claims)


def _subprotocols(ticket: str | None) -> list[str]:
    if ticket is None:
        return [WS_SUBPROTOCOL]
    return [WS_SUBPROTOCOL, f"{WS_TICKET_SUBPROTOCOL_PREFIX}{ticket}"]


def _connect(
    harness: Any,
    *,
    ticket: str | None = "valid",
    origin: str | None = ALLOWED_ORIGIN,
    subprotocols: list[str] | None = None,
) -> Any:
    """Build an unopened session. ``ticket="valid"`` resolves to a freshly signed one.

    The ``TestClient`` is intentionally not used as a context manager: entering it would run the app's
    lifespan, and this harness exists precisely to avoid that.
    """
    raw = _ticket(harness) if ticket == "valid" else ticket
    client = TestClient(harness.app)
    kwargs: dict[str, Any] = {
        "subprotocols": _subprotocols(raw) if subprotocols is None else subprotocols
    }
    if origin is not None:
        kwargs["headers"] = {"origin": origin}
    return client.websocket_connect("/ws/v1/stream", **kwargs)


def _session_open(**overrides: object) -> str:
    payload: dict[str, object] = {
        "type": "session.open",
        "call_ref": CALL_REF,
        "purpose_code": PURPOSE,
        "context_value_band": BAND,
    }
    payload.update(overrides)
    return json.dumps(payload)


def _expect_refused(connect: Any, code: str) -> None:
    """Assert the upgrade was refused *before* ``accept()``, with ``code``.

    Starlette raises :class:`WebSocketDisconnect` out of ``__enter__`` only when the first ASGI message
    the app produced was ``websocket.close``. So this passing is itself the proof that ``accept()`` was
    never called — the "authorization before accept" control, asserted rather than described. A handler
    that accepted first and closed afterwards would enter the block and fail on the ``pytest.fail``.
    """
    with pytest.raises(WebSocketDisconnect) as caught:
        with connect:
            pytest.fail(f"{code}: the upgrade was accepted; authorization ran after accept()")
    assert caught.value.code == ws_stream.CLOSE_CODES[code], f"{code}: wrong close code"
    assert caught.value.reason == ws_stream.CLOSE_REASONS[code], f"{code}: wrong close reason"


def _drain_to_close(socket: Any) -> tuple[int, str, list[dict[str, Any]]]:
    """Read until the server closes; return ``(close_code, reason, text_frames_seen)``.

    Uses the raw ``receive()`` and inspects the message type rather than relying on ``receive_text()``
    to raise. ``receive()`` returns the ``websocket.close`` message *without* raising, so a
    ``while True: receive()`` loop blocks forever on the next call — the queue is empty and nothing
    will ever arrive. Draining by message type rather than by exception is what keeps a failing
    assertion a failure instead of a hung CI job.
    """
    frames: list[dict[str, Any]] = []
    while True:
        message = socket.receive()
        if message["type"] == "websocket.close":
            return message.get("code", 1000), message.get("reason", ""), frames
        text = message.get("text")
        if text is not None:
            frames.append(json.loads(text))


def _assert_closed_with(socket: Any, code: str) -> list[dict[str, Any]]:
    """Assert an *accepted* stream was terminated with ``code`` — WS code, reason, and app code.

    The app code is the part that matters. ``PROTO_FRAME_SIZE``, ``PROTO_SEQUENCE`` and
    ``PROTO_FIRST_MESSAGE`` all close 1003, so a close-code-only assertion would pass for a handler
    that mixed them up, and "wrong byte length" would stop being a distinguishable exit criterion.
    """
    close_code, reason, frames = _drain_to_close(socket)
    assert close_code == ws_stream.CLOSE_CODES[code], f"{code}: wrong close code"
    assert reason == ws_stream.CLOSE_REASONS[code], f"{code}: wrong close reason"
    errors = [f for f in frames if f.get("type") == "error"]
    assert errors, f"{code}: no error frame was sent before the close"
    assert errors[-1]["code"] == code, f"expected app code {code}, got {errors[-1]['code']}"
    return frames


def _accept(socket: Any) -> dict[str, Any]:
    """Send a valid ``session.open`` and return the ``session.accepted`` event."""
    socket.send_text(_session_open())
    return json.loads(socket.receive_text())


# ---------------------------------------------------------------------------------------------------
# Exit criterion 1 — missing ticket
# ---------------------------------------------------------------------------------------------------
class TestMissingTicket:
    def test_no_ticket_subprotocol_is_refused(self, harness: Any) -> None:
        _expect_refused(_connect(harness, ticket=None), "AUTH_TICKET_MISSING")

    def test_no_subprotocols_at_all_is_refused(self, harness: Any) -> None:
        _expect_refused(_connect(harness, subprotocols=[]), "AUTH_TICKET_MISSING")

    def test_garbage_ticket_is_invalid_not_missing(self, harness: Any) -> None:
        """Missing and malformed are separate app codes on purpose: one is a client that never
        requested a ticket, the other is a client whose ticket failed the MAC. Collapsing them would
        make a signing-key rotation bug indistinguishable from a UI bug."""
        _expect_refused(_connect(harness, ticket="not-a-ticket"), "AUTH_TICKET_INVALID")

    def test_ticket_bound_to_another_subject_is_refused(self, harness: Any) -> None:
        """The session exists and the MAC is valid — it is the ``sub`` that does not own it. This is
        the case that would pass if the handler trusted ``peek_binding`` instead of ``verify``."""
        forged = _ticket(harness, sub="someone-else")
        _expect_refused(_connect(harness, ticket=forged), "AUTH_TICKET_INVALID")

    def test_ticket_bound_to_another_session_is_refused(self, harness: Any) -> None:
        forged = _ticket(harness, session_id="11111111-1111-4111-8111-111111111111")
        _expect_refused(_connect(harness, ticket=forged), "AUTH_TICKET_INVALID")

    def test_expired_ticket_is_refused(self, harness: Any) -> None:
        _expect_refused(_connect(harness, ticket=_ticket(harness, ttl=-1)), "AUTH_TICKET_INVALID")

    def test_replayed_ticket_is_refused(self, harness: Any) -> None:
        """Single use. The second connection must fail even though the ticket is still inside its TTL
        and its MAC still verifies — otherwise a captured ticket is a reusable stream credential."""
        raw = _ticket(harness)
        with _connect(harness, ticket=raw) as socket:
            _accept(socket)
            socket.close(1000)
        # The portal thread is joined on ``with`` exit, so the handler's ``finally`` has already run
        # and released the session. No sleep or retry loop needed.
        assert harness.record.streaming is False
        _expect_refused(_connect(harness, ticket=raw), "AUTH_TICKET_INVALID")


# ---------------------------------------------------------------------------------------------------
# Exit criterion 2 — wrong Origin
# ---------------------------------------------------------------------------------------------------
class TestOrigin:
    def test_non_allow_listed_origin_is_refused(self, harness: Any) -> None:
        _expect_refused(_connect(harness, origin=DENIED_ORIGIN), "AUTH_ORIGIN_DENIED")

    def test_absent_origin_is_refused(self, harness: Any) -> None:
        """A WebSocket upgrade carrying no ``Origin`` is not a browser. Refusing rather than defaulting
        to allow is what makes the allow-list a control instead of a preference."""
        _expect_refused(_connect(harness, origin=None), "AUTH_ORIGIN_DENIED")

    def test_a_prefix_match_is_not_enough(self, harness: Any) -> None:
        """``https://demo.example.invalid.attacker.test`` starts with the allowed origin. If the check
        were a ``startswith`` this would be admitted and the allow-list would be decorative."""
        _expect_refused(
            _connect(harness, origin=f"{ALLOWED_ORIGIN}.attacker.test"), "AUTH_ORIGIN_DENIED"
        )

    def test_missing_ticket_outranks_a_denied_origin(self, harness: Any) -> None:
        """Precedence is observable, so pin it. Ticket extraction runs first inside the shared
        ``try``; a future reorder should be a deliberate change with a failing test, not a silent
        change in which code a client sees."""
        _expect_refused(_connect(harness, ticket=None, origin=DENIED_ORIGIN), "AUTH_TICKET_MISSING")


# ---------------------------------------------------------------------------------------------------
# Exit criterion 3 — duplicate sequence
# ---------------------------------------------------------------------------------------------------
class TestSequence:
    def test_duplicate_sequence_closes_the_stream(self, harness: Any) -> None:
        with _connect(harness) as socket:
            _accept(socket)
            socket.send_bytes(make_frame(0))
            socket.send_bytes(make_frame(0))  # the same sequence, replayed
            _assert_closed_with(socket, "PROTO_SEQUENCE")

    def test_a_gap_is_the_same_error_as_a_duplicate(self, harness: Any) -> None:
        """Both mean the evidence sequence has a hole, and a 2.56 s window assembled across a hole is
        a window of audio that was never contiguous. One app code, deliberately."""
        with _connect(harness) as socket:
            _accept(socket)
            socket.send_bytes(make_frame(0))
            socket.send_bytes(make_frame(2))
            _assert_closed_with(socket, "PROTO_SEQUENCE")

    def test_stream_must_start_at_zero(self, harness: Any) -> None:
        with _connect(harness) as socket:
            _accept(socket)
            socket.send_bytes(make_frame(1))
            _assert_closed_with(socket, "PROTO_SEQUENCE")

    def test_out_of_order_delivery_is_refused_not_reordered(self, harness: Any) -> None:
        """There is no reorder buffer, on purpose: buffering would let the decision depend on arrival
        timing, and the audit trail could then not be replayed deterministically."""
        with _connect(harness) as socket:
            _accept(socket)
            socket.send_bytes(make_frame(0))
            socket.send_bytes(make_frame(1))
            socket.send_bytes(make_frame(3))
            _assert_closed_with(socket, "PROTO_SEQUENCE")


# ---------------------------------------------------------------------------------------------------
# Exit criterion 4 — wrong byte length
# ---------------------------------------------------------------------------------------------------
class TestFrameSize:
    @pytest.mark.parametrize("length", [0, 1, 8, 647, 649, 1296])
    def test_any_length_but_the_contract_size_is_refused(self, harness: Any, length: int) -> None:
        """647 and 649 are the cases that matter: an off-by-one frame is never padded or truncated
        (rules.md R-24). 1296 is exactly two frames, which is how a client that batches gets caught
        rather than silently half-processed. 8 is a header with no payload."""
        assert length != WS_FRAME_BYTES
        with _connect(harness) as socket:
            _accept(socket)
            socket.send_bytes(b"\x00" * length)
            _assert_closed_with(socket, "PROTO_FRAME_SIZE")

    def test_a_correctly_sized_frame_is_accepted(self, harness: Any) -> None:
        """The control against a suite that passes by rejecting everything. If this fails, every
        assertion above it is worthless."""
        with _connect(harness) as socket:
            _accept(socket)
            for seq in range(4):
                socket.send_bytes(make_frame(seq))
            socket.close(1000)
        assert harness.audit.appended == []  # 4 frames is 80 ms — nowhere near a 2.56 s window


# ---------------------------------------------------------------------------------------------------
# Kept beyond the plan's four — phases.md §2.4
# ---------------------------------------------------------------------------------------------------
class TestPurposeBinding:
    """``purpose_code`` is bound at ``POST /api/v1/sessions``, before any audio exists; ``session.open``
    may only echo it. A mismatch is an attempt to re-declare purpose on the audio channel, which would
    make purpose a client-supplied label rather than a control."""

    def test_mismatched_purpose_is_refused(self, harness: Any) -> None:
        with _connect(harness) as socket:
            socket.send_text(_session_open(purpose_code="account_recovery"))
            _assert_closed_with(socket, "PROTO_PURPOSE_MISMATCH")

    def test_mismatched_value_band_is_refused(self, harness: Any) -> None:
        with _connect(harness) as socket:
            socket.send_text(_session_open(context_value_band="low"))
            _assert_closed_with(socket, "PROTO_PURPOSE_MISMATCH")

    def test_no_audit_row_is_written_for_a_rejected_handshake(self, harness: Any) -> None:
        """An evidence trail containing events for a stream that never carried audio is a false
        record — and it is the kind of falseness that only surfaces when a human reads the table."""
        with _connect(harness) as socket:
            socket.send_text(_session_open(purpose_code="account_recovery"))
            _assert_closed_with(socket, "PROTO_PURPOSE_MISMATCH")
        assert harness.audit.appended == []


class TestOversizedText:
    def test_text_frame_over_the_cap_is_refused(self, harness: Any) -> None:
        """The cap is enforced by the application, not the transport. Uvicorn's ``--ws-max-size`` is
        deliberately set well above ``MAX_TEXT_FRAME_BYTES`` (memory.md BUG-3) so this close code is
        reachable at all: a tighter transport guard would close 1009 before application code ran, and
        this test would be asserting a property of uvicorn."""
        oversized = "x" * (MAX_TEXT_FRAME_BYTES + 64)
        with _connect(harness) as socket:
            socket.send_text(json.dumps({"type": "session.open", "pad": oversized}))
            _assert_closed_with(socket, "PROTO_PAYLOAD_TOO_LARGE")

    def test_size_is_checked_before_the_json_is_parsed(self, harness: Any) -> None:
        """Deliberately not valid JSON. If the handler parsed first it would report
        ``PROTO_FIRST_MESSAGE``, and it would have spent the parse on an oversized payload — the cheap
        denial of service the cap exists to prevent."""
        with _connect(harness) as socket:
            socket.send_text("{" + "x" * (MAX_TEXT_FRAME_BYTES + 64))
            _assert_closed_with(socket, "PROTO_PAYLOAD_TOO_LARGE")


class TestFirstMessage:
    def test_binary_first_is_a_protocol_error_not_buffered_audio(self, harness: Any) -> None:
        """There is no bound purpose yet, so there is nothing to attach a decision to. Buffering it
        would mean audio existed in the process before consent and purpose were confirmed."""
        with _connect(harness) as socket:
            socket.send_bytes(make_frame(0))
            _assert_closed_with(socket, "PROTO_FIRST_MESSAGE")

    @pytest.mark.parametrize(
        ("label", "payload"),
        [
            ("not json", "not json at all"),
            ("json array", "[]"),
            ("json string", '"a string"'),
            ("json null", "null"),
            ("wrong type", '{"type":"session.resume"}'),
            ("no type", '{"call_ref":"' + CALL_REF + '"}'),
            ("no call_ref", '{"type":"session.open"}'),
        ],
    )
    def test_malformed_first_message_is_refused(
        self, harness: Any, label: str, payload: str
    ) -> None:
        with _connect(harness) as socket:
            socket.send_text(payload)
            _assert_closed_with(socket, "PROTO_FIRST_MESSAGE")

    @pytest.mark.privacy
    def test_unknown_field_is_refused_rather_than_ignored(self, harness: Any) -> None:
        """An ignored extra key is somewhere a client could put a transcript or a phone number on the
        audio channel (rules.md R-16). Tolerating unknown fields is the permissive default that makes
        that leak possible, so the handler refuses instead."""
        with _connect(harness) as socket:
            socket.send_text(_session_open(transcript="the card number is ..."))
            _assert_closed_with(socket, "PROTO_FIRST_MESSAGE")

    @pytest.mark.privacy
    def test_raw_reference_where_a_pseudonym_belongs_is_refused(self, harness: Any) -> None:
        """The last boundary at which a client-side mistake can be caught before a raw caller
        reference reaches server-side session state (rules.md R-16)."""
        with _connect(harness) as socket:
            socket.send_text(_session_open(call_ref="+919812345678"))
            _assert_closed_with(socket, "PROTO_FIRST_MESSAGE")

    @pytest.mark.privacy
    def test_a_well_formed_pseudonym_for_a_different_session_is_refused(self, harness: Any) -> None:
        """Shape-valid, but not *this* session's pseudonym. Checking only the shape would let a client
        attach its audio to a ``call_ref`` the server never issued."""
        with _connect(harness) as socket:
            socket.send_text(_session_open(call_ref="c" * 64))
            _assert_closed_with(socket, "PROTO_FIRST_MESSAGE")

    def test_text_after_session_open_is_refused(self, harness: Any) -> None:
        """After the handshake the channel is binary. A text frame here is a client bug or an attempt
        to re-declare purpose mid-stream."""
        with _connect(harness) as socket:
            _accept(socket)
            socket.send_text(_session_open())
            _assert_closed_with(socket, "PROTO_FIRST_MESSAGE")


class TestSessionExclusivity:
    def test_second_concurrent_stream_on_one_session_is_refused(self, harness: Any) -> None:
        """Two streams interleaving into one evidence sequence and one hash chain would make the audit
        trail describe a call that never happened."""
        with _connect(harness) as first:
            _accept(first)
            _expect_refused(_connect(harness), "SESSION_ALREADY_STREAMING")
            first.close(1000)

    def test_capacity_is_refused_not_queued(self, harness: Any) -> None:
        """rules.md R-20. A queued stream would sit accumulating latency and then report a p95 that
        describes waiting rather than scoring."""
        harness.app.state.settings = _settings(max_streams=0)
        _expect_refused(_connect(harness), "BACKPRESSURE_REJECT")


# ---------------------------------------------------------------------------------------------------
# The mapping tables themselves
# ---------------------------------------------------------------------------------------------------
class TestCloseCodeTables:
    def test_every_app_code_has_both_a_code_and_a_reason(self) -> None:
        """Equal key sets. An app code in one table but not the other closes with either a default
        1011 or an empty reason, and the client-side diagnosis for both is "the server crashed"."""
        assert set(ws_stream.CLOSE_CODES) == set(ws_stream.CLOSE_REASONS)

    @pytest.mark.privacy
    def test_no_reason_can_interpolate_client_input(self) -> None:
        """rules.md R-17. Close reasons are recorded by proxies and browsers, so a reason built with a
        format placeholder is a documented path for a caller reference to escape into a log nobody
        considers part of the system."""
        for code, reason in ws_stream.CLOSE_REASONS.items():
            assert "%" not in reason, code
            assert "{" not in reason and "}" not in reason, code
            assert reason and reason == reason.strip(), code

    def test_reasons_fit_a_websocket_close_frame(self) -> None:
        """RFC 6455 caps the close payload at 125 bytes, two of which are the code. An over-long
        reason is truncated or drops the frame — either way the diagnostic is lost exactly when it is
        needed."""
        for code, reason in ws_stream.CLOSE_REASONS.items():
            assert len(reason.encode("utf-8")) <= 123, code

    def test_authorization_failures_do_not_report_as_server_faults(self) -> None:
        """1011 means "the server failed". Reporting a rejected ticket that way sends a client into a
        retry loop against a server that is working correctly and refusing it on purpose."""
        for code in ("AUTH_TICKET_MISSING", "AUTH_TICKET_INVALID", "AUTH_ORIGIN_DENIED"):
            assert ws_stream.CLOSE_CODES[code] == 1008, code

    def test_protocol_violations_report_as_unsupported_data(self) -> None:
        for code in ("PROTO_FRAME_SIZE", "PROTO_SEQUENCE", "PROTO_FIRST_MESSAGE"):
            assert ws_stream.CLOSE_CODES[code] == 1003, code

    def test_capacity_is_the_try_again_code(self) -> None:
        """1013 tells a client to retry later; 1008 tells it to stop. Getting this wrong at capacity
        means either a client that gives up on a transient limit or one that hammers a closed door."""
        assert ws_stream.CLOSE_CODES["BACKPRESSURE_REJECT"] == 1013

    def test_close_codes_are_in_a_valid_range(self) -> None:
        for code, value in ws_stream.CLOSE_CODES.items():
            assert 1000 <= value <= 4999, code

    def test_no_action_vocabulary_leaks_into_a_close_reason(self) -> None:
        """rules.md R-07, applied to close reasons.

        R-07 names enums, config values, DB CHECK constraints, API schemas and UI strings. Close
        reasons are none of those literally, so the scope is a judgment call — and the answer is that
        they are in scope, because a close reason is *more* exposed than a UI string, not less: it is
        recorded by proxies, surfaced in browser devtools, and read by whoever is debugging a failed
        demo. A system whose whole discipline is "we never refuse a person, we ask for verification"
        should not describe anything as rejected in the one string nobody reviews for wording.

        This caught ``AUTH_TICKET_INVALID: "stream ticket rejected"`` on its first run. It now reads
        "stream ticket not valid", which also pairs properly with "stream ticket not offered".

        The unreachable-fallback case is covered indirectly:
        :meth:`test_every_app_code_has_both_a_code_and_a_reason` is what guarantees
        ``CLOSE_REASONS.get(code, ...)`` in ``_close`` never falls through for a real app code.
        """
        for code, reason in ws_stream.CLOSE_REASONS.items():
            lowered = reason.lower()
            for word in ("approve", "deny", "allow", "block", "reject"):
                assert word not in lowered, f"{code}: {reason!r}"


@pytest.mark.privacy
class TestNoResidueOnRejectedPaths:
    def test_a_malformed_frame_never_reaches_the_scorer(self, harness: Any) -> None:
        """``_FakeScorer`` raises on any call, so this passing means the frame was refused before
        inference rather than scored and then discarded."""
        with _connect(harness) as socket:
            _accept(socket)
            socket.send_bytes(b"\x00" * (WS_FRAME_BYTES - 1))
            _assert_closed_with(socket, "PROTO_FRAME_SIZE")
        assert harness.audit.appended == []

    def test_the_stream_slot_is_released_after_a_protocol_error(self, harness: Any) -> None:
        """The ``finally`` block clears the ring buffer, releases the session, and drops the audit
        chain state. If it did not, a client that sent one malformed frame would be locked out of its
        own session for the 900 s TTL, and the demo recovery path would be "start over"."""
        with _connect(harness) as socket:
            _accept(socket)
            socket.send_bytes(b"\x00")
            _assert_closed_with(socket, "PROTO_FRAME_SIZE")
        assert harness.record.streaming is False
        assert harness.app.state.live_streams == 0
        assert harness.audit.forgotten == [str(harness.record.session_id)]

    def test_the_slot_is_never_claimed_by_a_refused_handshake(self, harness: Any) -> None:
        """A refusal happens before ``begin_stream``, so nothing should have been claimed. If the
        counter drifted up here, ``max_concurrent_streams`` would decay under a flood of bad tickets —
        an unauthenticated client could exhaust capacity."""
        _expect_refused(_connect(harness, ticket="not-a-ticket"), "AUTH_TICKET_INVALID")
        assert harness.app.state.live_streams == 0
        assert harness.record.streaming is False
        assert harness.audit.forgotten == []


class TestAcceptedHandshake:
    def test_session_accepted_carries_the_parity_set(self, harness: Any) -> None:
        """The positive control for the file, and the event the PWA needs in order to state which tier
        and which artifacts produced what the operator is about to see."""
        with _connect(harness) as socket:
            event = _accept(socket)
            socket.close(1000)
        assert event["type"] == "session.accepted"
        assert event["session_id"] == str(harness.record.session_id)
        for field in (
            "policy_version",
            "model_version",
            "calibration_version",
            "deployment_profile",
            "execution_provider",
            "detector_mode",
            "artifact_state",
        ):
            assert field in event, field

    def test_mock_mode_is_declared_in_the_handshake(self, harness: Any) -> None:
        """A mock score presented as a measurement is the worst failure this system can have, so the
        client is told on the very first message rather than having to infer it."""
        with _connect(harness) as socket:
            event = _accept(socket)
            socket.close(1000)
        assert event["detector_mode"] == "MOCK_SMOKE"
        assert event["artifact_state"] != "policy_eligible"

    def test_the_ticket_is_not_echoed_in_the_negotiated_subprotocol(self, harness: Any) -> None:
        """Echoing the offered subprotocols back would place a live credential in a response header,
        where proxies and browser devtools record it. Only the contract version is negotiated."""
        with _connect(harness) as socket:
            _accept(socket)
            negotiated = socket.accepted_subprotocol
            socket.close(1000)
        assert negotiated == WS_SUBPROTOCOL
        assert WS_TICKET_SUBPROTOCOL_PREFIX not in (negotiated or "")
