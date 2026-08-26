"""WebSocket stream tickets — single-use, 60 s TTL, bound to session and subject (decision D-6).

This exists so the bearer token never appears in a WebSocket URL, query string, or cookie. Browsers
cannot set headers on a WebSocket handshake, so the two common workarounds are a token in the query
string (which lands in every access log and CloudFront log) or a cookie (which reintroduces CSRF
surface). A short-lived single-use ticket carried in ``Sec-WebSocket-Protocol`` avoids both.

Signing and verification are pure. Replay tracking needs state, so it lives in a separate
:class:`ReplayCache` that takes its clock by injection — keeping ``sign``/``verify`` testable without
a clock, and making the replay window explicit rather than incidental.
"""

from __future__ import annotations

import base64
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import Final

from app.constants import TICKET_TTL_SECONDS, WS_TICKET_SUBPROTOCOL_PREFIX

_DOMAIN: Final[bytes] = b"sih26104/stream_ticket/v1\x00"
_MAX_TICKET_BYTES: Final[int] = 1024


class TicketError(Exception):
    """Ticket rejected. ``code`` is the app-level code from technical-design.md section 2.5."""

    __slots__ = ("code",)

    def __init__(self, code: str = "AUTH_TICKET_INVALID"):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class TicketClaims:
    session_id: str
    sub: str
    jti: str
    exp: int


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64u_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def sign(signing_key: bytes, claims: TicketClaims) -> str:
    """Produce a ``<payload>.<mac>`` ticket, both parts url-safe base64 without padding.

    Not a JWT: a JWT here would invite the header-declared-algorithm class of bug for a token with
    exactly one issuer, one algorithm, and one verifier. A fixed-format MAC has no algorithm field
    to confuse.
    """
    payload = json.dumps(
        {"sid": claims.session_id, "sub": claims.sub, "jti": claims.jti, "exp": claims.exp},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    mac = hmac.new(signing_key, _DOMAIN + payload, sha256).digest()
    return f"{_b64u_encode(payload)}.{_b64u_encode(mac)}"


def verify(
    signing_key: bytes,
    ticket: str,
    *,
    now: int,
    expected_session_id: str,
    expected_sub: str,
) -> TicketClaims:
    """Verify signature, expiry, and binding.

    The MAC is checked BEFORE the payload is parsed as JSON, so unauthenticated bytes never reach
    the JSON decoder.

    Raises:
        TicketError: on any failure. One error code for every cause — a caller learns "invalid",
            never which check failed, since distinguishing them turns this into an oracle.
    """
    if not ticket or len(ticket) > _MAX_TICKET_BYTES:
        raise TicketError()

    head, _, tail = ticket.partition(".")
    if not head or not tail:
        raise TicketError()

    try:
        payload = _b64u_decode(head)
        mac = _b64u_decode(tail)
    except (ValueError, TypeError) as exc:
        raise TicketError() from exc

    expected_mac = hmac.new(signing_key, _DOMAIN + payload, sha256).digest()
    if not hmac.compare_digest(mac, expected_mac):
        raise TicketError()

    try:
        data = json.loads(payload)
        claims = TicketClaims(
            session_id=str(data["sid"]),
            sub=str(data["sub"]),
            jti=str(data["jti"]),
            exp=int(data["exp"]),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise TicketError() from exc

    if claims.exp <= now:
        raise TicketError()
    if not hmac.compare_digest(claims.session_id, expected_session_id):
        raise TicketError()
    if not hmac.compare_digest(claims.sub, expected_sub):
        raise TicketError()

    return claims


def peek_binding(ticket: str) -> tuple[str, str]:
    """Read the ``(session_id, sub)`` a ticket *claims*, WITHOUT verifying it.

    :func:`verify` binds against an expected session and subject, but at the WebSocket handshake those
    values are only knowable from the ticket itself — there is no header to carry them. So the caller
    peeks here to learn what to bind to, then calls :func:`verify`, which re-reads the same claims from
    MAC-authenticated bytes.

    The return value of this function is therefore an UNTRUSTED HINT. Never use it for an
    authorization decision, a database lookup that has side effects, or a log line. The only correct
    use is as the ``expected_*`` arguments to :func:`verify` immediately afterwards, where a forged
    value simply produces a MAC mismatch.
    """
    head, _, tail = ticket.partition(".")
    if not head or not tail:
        raise TicketError()
    try:
        data = json.loads(_b64u_decode(head))
        return str(data["sid"]), str(data["sub"])
    except (ValueError, TypeError, KeyError) as exc:
        raise TicketError() from exc


def extract_from_subprotocols(offered: list[str] | tuple[str, ...] | None) -> str:
    """Pull the ticket out of the offered ``Sec-WebSocket-Protocol`` values.

    Raises:
        TicketError: ``AUTH_TICKET_MISSING`` if no ``sih-ticket.`` entry was offered.
    """
    for value in offered or ():
        candidate = value.strip()
        if candidate.startswith(WS_TICKET_SUBPROTOCOL_PREFIX):
            return candidate[len(WS_TICKET_SUBPROTOCOL_PREFIX) :]
    raise TicketError("AUTH_TICKET_MISSING")


class ReplayCache:
    """Remembers spent ``jti`` values for as long as a ticket could still be valid.

    Bounded by construction: entries are evicted once they pass their own expiry, and since every
    ticket lives at most ``TICKET_TTL_SECONDS``, the cache cannot grow without bound even under a
    flood. That matters because an unbounded replay cache is a memory-exhaustion path reachable by
    an unauthenticated client.

    Single-process only. Phase 4 moves this to a shared store when the Gateway runs more than one
    task; until then ``desired_count`` is 1 and this is correct rather than convenient.
    """

    __slots__ = ("_clock", "_spent", "_ttl")

    def __init__(self, clock: Callable[[], int], ttl_seconds: int = TICKET_TTL_SECONDS):
        self._spent: dict[str, int] = {}
        self._clock = clock
        self._ttl = ttl_seconds

    def spend(self, jti: str, exp: int) -> None:
        """Mark a ticket as used.

        Raises:
            TicketError: if it was already spent. A ticket presented twice is a replayed stream,
                which the blueprint's threat table requires as a negative test.
        """
        now = self._clock()
        self._evict(now)
        if jti in self._spent:
            raise TicketError()
        self._spent[jti] = min(exp, now + self._ttl)

    def _evict(self, now: int) -> None:
        if not self._spent:
            return
        for key in [k for k, expiry in self._spent.items() if expiry <= now]:
            del self._spent[key]

    def __len__(self) -> int:
        return len(self._spent)
