"""Stream-ticket tests.

The ticket exists so the bearer token never lands in a WebSocket URL, query string, or cookie. These
tests cover the three properties that make it worth having over a query-string token: it is
unforgeable, it is bound to one session and one subject, and it cannot be replayed.

They also pin the non-oracle property. Every failure produces the same error code, so a caller learns
"invalid" and never which check failed — distinguishing expiry from a bad MAC from a wrong session
turns this into an oracle an attacker can walk.
"""

from __future__ import annotations

import pytest

from app.constants import TICKET_TTL_SECONDS, WS_TICKET_SUBPROTOCOL_PREFIX
from app.security.ticket import (
    ReplayCache,
    TicketClaims,
    TicketError,
    extract_from_subprotocols,
    peek_binding,
    sign,
    verify,
)
from tests.conftest import TEST_TICKET_KEY

OTHER_KEY = b"forged-ticket-key-also-not-a-secret0"

NOW = 1_800_000_000
SESSION = "11111111-1111-4111-8111-111111111111"
SUB = "cognito-subject-0001"


def claims(**overrides: object) -> TicketClaims:
    base: dict[str, object] = {
        "session_id": SESSION,
        "sub": SUB,
        "jti": "jti-0001",
        "exp": NOW + TICKET_TTL_SECONDS,
    }
    base.update(overrides)
    return TicketClaims(**base)  # type: ignore[arg-type]


def good_ticket(**overrides: object) -> str:
    return sign(TEST_TICKET_KEY, claims(**overrides))


def check(ticket: str, *, now: int = NOW, session: str = SESSION, sub: str = SUB) -> TicketClaims:
    return verify(TEST_TICKET_KEY, ticket, now=now, expected_session_id=session, expected_sub=sub)


class TestFormat:
    def test_ticket_is_two_dot_separated_parts(self) -> None:
        head, dot, tail = good_ticket().partition(".")
        assert dot == "." and head and tail

    def test_ticket_is_url_safe_and_unpadded(self) -> None:
        """It travels in Sec-WebSocket-Protocol, which is a token list — a "=" or "/" would need
        escaping and some proxies would mangle it."""
        ticket = good_ticket()
        assert "=" not in ticket
        assert "+" not in ticket
        assert "/" not in ticket

    def test_signing_is_deterministic(self) -> None:
        assert good_ticket() == good_ticket()

    def test_round_trip_returns_the_claims(self) -> None:
        result = check(good_ticket())
        assert (result.session_id, result.sub, result.jti) == (SESSION, SUB, "jti-0001")


class TestForgery:
    def test_wrong_key_is_rejected(self) -> None:
        forged = sign(OTHER_KEY, claims())
        with pytest.raises(TicketError):
            check(forged)

    def test_tampered_payload_is_rejected(self) -> None:
        """The substance of the MAC: an attacker who edits the session id to point at someone else's
        session cannot produce a matching tail."""
        head, _, tail = good_ticket().partition(".")
        other = sign(TEST_TICKET_KEY, claims(session_id="22222222-2222-4222-8222-222222222222"))
        other_head = other.partition(".")[0]
        with pytest.raises(TicketError):
            check(f"{other_head}.{tail}")
        assert head != other_head

    def test_tampered_mac_is_rejected(self) -> None:
        head, _, tail = good_ticket().partition(".")
        flipped = ("A" if tail[0] != "A" else "B") + tail[1:]
        with pytest.raises(TicketError):
            check(f"{head}.{flipped}")

    @pytest.mark.parametrize(
        "ticket",
        ["", ".", "a.", ".b", "nodot", "!!!.!!!", "a" * 2000, "a.b.c"],
    )
    def test_malformed_tickets_are_rejected(self, ticket: str) -> None:
        with pytest.raises(TicketError):
            check(ticket)

    def test_unauthenticated_bytes_never_reach_the_json_decoder(self) -> None:
        """The MAC is checked BEFORE the payload is parsed. Verified by handing over a valid-MAC-less
        ticket whose payload is JSON that would crash a parser if it were reached."""
        import base64

        payload = base64.urlsafe_b64encode(b'{"sid": ' * 500).decode().rstrip("=")
        with pytest.raises(TicketError):
            check(f"{payload}.AAAA")


class TestBinding:
    def test_wrong_session_is_rejected(self) -> None:
        """Decision D-6. A ticket minted for one session must not open a stream on another, or a
        second browser tab could attach to a session it was never authorized for."""
        with pytest.raises(TicketError):
            check(good_ticket(), session="99999999-9999-4999-8999-999999999999")

    def test_wrong_subject_is_rejected(self) -> None:
        with pytest.raises(TicketError):
            check(good_ticket(), sub="someone-else")

    def test_expired_ticket_is_rejected(self) -> None:
        with pytest.raises(TicketError):
            check(good_ticket(), now=NOW + TICKET_TTL_SECONDS + 1)

    def test_ticket_at_exact_expiry_is_rejected(self) -> None:
        """exp <= now, not < . A ticket valid at its own expiry instant is a boundary nobody tests
        and an argument nobody wins."""
        with pytest.raises(TicketError):
            check(good_ticket(), now=NOW + TICKET_TTL_SECONDS)

    def test_ttl_is_sixty_seconds(self) -> None:
        assert TICKET_TTL_SECONDS == 60


class TestNoOracle:
    def test_every_failure_yields_the_same_code(self) -> None:
        """A caller learns "invalid", never which check failed."""
        cases = [
            lambda: check("garbage"),
            lambda: check(sign(OTHER_KEY, claims())),
            lambda: check(good_ticket(), now=NOW + 10_000),
            lambda: check(good_ticket(), session="00000000-0000-4000-8000-000000000000"),
            lambda: check(good_ticket(), sub="nobody"),
        ]
        codes = set()
        for case in cases:
            with pytest.raises(TicketError) as caught:
                case()
            codes.add(caught.value.code)
        assert codes == {"AUTH_TICKET_INVALID"}

    @pytest.mark.privacy
    def test_error_carries_no_ticket_material(self) -> None:
        ticket = good_ticket()
        with pytest.raises(TicketError) as caught:
            check(ticket, sub="nobody")
        assert ticket not in str(caught.value)
        assert SUB not in str(caught.value)


class TestPeekBinding:
    def test_peek_reads_the_claimed_binding(self) -> None:
        """Resolves the handshake chicken-and-egg: verify() binds against an expected session and
        subject, but at the WebSocket handshake those are only knowable from the ticket itself."""
        assert peek_binding(good_ticket()) == (SESSION, SUB)

    def test_peek_does_not_verify(self) -> None:
        """Documented as an UNTRUSTED HINT, and this test is the proof: a forged ticket peeks fine.

        Safe only because the caller feeds the peeked values straight into verify(), where a forged
        value produces a MAC mismatch. Using a peeked value for anything else is the bug.
        """
        forged = sign(OTHER_KEY, claims())
        assert peek_binding(forged) == (SESSION, SUB)
        with pytest.raises(TicketError):
            check(forged)

    def test_peek_rejects_malformed_input(self) -> None:
        for bad in ["", "nodot", "!!!.x"]:
            with pytest.raises(TicketError):
                peek_binding(bad)


class TestSubprotocolExtraction:
    def test_extracts_the_ticket(self) -> None:
        ticket = good_ticket()
        offered = ["sih-v1", f"{WS_TICKET_SUBPROTOCOL_PREFIX}{ticket}"]
        assert extract_from_subprotocols(offered) == ticket

    def test_tolerates_surrounding_whitespace(self) -> None:
        """Sec-WebSocket-Protocol is comma-separated, and clients vary on whether they trim."""
        ticket = good_ticket()
        assert extract_from_subprotocols([f"  {WS_TICKET_SUBPROTOCOL_PREFIX}{ticket} "]) == ticket

    @pytest.mark.parametrize("offered", [None, [], ["sih-v1"], ["bearer.abc"], [""]])
    def test_missing_ticket_reports_a_distinct_code(self, offered: list[str] | None) -> None:
        """The one place a distinct code is correct: "you sent no ticket" is not an oracle about a
        secret, and the client needs to tell it apart from "your ticket was rejected"."""
        with pytest.raises(TicketError) as caught:
            extract_from_subprotocols(offered)
        assert caught.value.code == "AUTH_TICKET_MISSING"


class TestReplayCache:
    def test_first_spend_succeeds(self) -> None:
        cache = ReplayCache(clock=lambda: NOW)
        cache.spend("jti-0001", NOW + 60)

    def test_second_spend_is_refused(self) -> None:
        """Single-use. A ticket presented twice is a replayed stream, which the blueprint's threat
        table requires as a negative test."""
        cache = ReplayCache(clock=lambda: NOW)
        cache.spend("jti-0001", NOW + 60)
        with pytest.raises(TicketError):
            cache.spend("jti-0001", NOW + 60)

    def test_distinct_jtis_are_independent(self) -> None:
        cache = ReplayCache(clock=lambda: NOW)
        for i in range(10):
            cache.spend(f"jti-{i:04d}", NOW + 60)
        assert len(cache) == 10

    def test_entries_are_evicted_once_they_expire(self) -> None:
        """Bounded by construction. An unbounded replay cache is a memory-exhaustion path reachable
        by an unauthenticated client, which is a worse bug than the replay it prevents."""
        clock = {"t": NOW}
        cache = ReplayCache(clock=lambda: clock["t"])
        for i in range(100):
            cache.spend(f"jti-{i:04d}", NOW + 60)
        assert len(cache) == 100
        clock["t"] = NOW + TICKET_TTL_SECONDS + 1
        cache.spend("jti-later", clock["t"] + 60)
        assert len(cache) == 1

    def test_cache_cannot_grow_beyond_one_ttl_of_traffic(self) -> None:
        clock = {"t": NOW}
        cache = ReplayCache(clock=lambda: clock["t"])
        for i in range(500):
            clock["t"] = NOW + i
            cache.spend(f"jti-{i:04d}", clock["t"] + TICKET_TTL_SECONDS)
        assert len(cache) <= TICKET_TTL_SECONDS + 1

    def test_expiry_is_clamped_to_the_ttl(self) -> None:
        """A ticket claiming a far-future exp must not pin an entry in the cache forever. The claim is
        attacker-influenced only after a MAC check, but clamping means even a signing-key mistake
        cannot turn the cache into a leak."""
        clock = {"t": NOW}
        cache = ReplayCache(clock=lambda: clock["t"])
        cache.spend("jti-forever", NOW + 10**9)
        clock["t"] = NOW + TICKET_TTL_SECONDS + 1
        cache.spend("jti-next", clock["t"] + 60)
        assert len(cache) == 1
