"""Session registry — in-memory, single-process (Phase 1).

Holds the server-side session record created by ``POST /api/v1/sessions``: the pseudonym, the bound
``purpose_code`` and ``context_value_band``, and the owning principal. The WSS handshake reads it to
verify that ``session.open`` echoes the purpose that was bound *before any audio existed* (decision
D-4), which is what makes purpose binding a control rather than a client-supplied label.

**Deliberately not the database.** A session record is live state for the duration of one stream; the
durable artifact is the audit trail. Putting live state in Postgres would add a table whose columns
would then need to be argued past the structural deny-list, for data that is worthless five minutes
later.

**Single-process, and that is currently correct.** ``desired_count`` is 1 for the five-day window
(rules.md R-32), so there is no second task to share state with. Phase 4 moves this to a shared store
when the Gateway scales — that is recorded in phases.md, and it is a real change, not a config flip:
:class:`SessionRegistry` is the seam it happens behind. This module is not a load-bearing decision
about scale; it is the smallest thing that is honest about the current one.

Not marked as a deviation from technical-design.md section 4.1 by accident — see memory.md, which records that
this module was added during Phase-1 implementation because the WSS purpose check needs somewhere to
read the bound purpose from.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

#: A session must be opened, ticketed, and streamed within this window. Long enough for a human to
#: read a consent notice and click; short enough that abandoned sessions do not accumulate.
SESSION_TTL_SECONDS: Final[int] = 900


class SessionError(Exception):
    """Session not found, expired, or not owned by this principal.

    One error for all three: distinguishing "expired" from "not yours" tells a caller whether a
    session id exists.
    """


class SessionAlreadyStreaming(Exception):
    """A live stream already exists for this session. App code ``SESSION_ALREADY_STREAMING``."""

    __slots__ = ("code",)

    def __init__(self) -> None:
        super().__init__("SESSION_ALREADY_STREAMING")
        self.code = "SESSION_ALREADY_STREAMING"


@dataclass(slots=True)
class SessionRecord:
    """Server-side session state.

    Note what is absent: the raw ``client_call_ref``. It is pseudonymized in the request handler and
    the raw value is never stored here, so there is no field for it to be read back out of
    (rules.md R-16).
    """

    session_id: UUID
    call_ref: str  # HMAC pseudonym only
    purpose_code: str
    context_value_band: str
    owner_sub: str
    tenant_id: str
    created_at: datetime
    expires_at: datetime
    consent_acknowledged: bool
    streaming: bool = field(default=False)
    windows_scored: int = field(default=0)

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


class SessionRegistry:
    """Create, look up, and expire session records."""

    __slots__ = ("_clock", "_max_sessions", "_records", "_ttl")

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
        ttl_seconds: int = SESSION_TTL_SECONDS,
        max_sessions: int = 256,
    ):
        self._records: dict[UUID, SessionRecord] = {}
        self._clock = clock
        self._ttl = ttl_seconds
        self._max_sessions = max_sessions

    def create(
        self,
        *,
        call_ref: str,
        purpose_code: str,
        context_value_band: str,
        owner_sub: str,
        tenant_id: str,
        consent_acknowledged: bool,
    ) -> SessionRecord:
        now = self._clock()
        self._expire(now)
        if len(self._records) >= self._max_sessions:
            # Refuse rather than evict. Evicting someone else's live session to make room for a new
            # one would terminate a stream mid-decision.
            raise SessionError("session capacity reached")

        record = SessionRecord(
            session_id=uuid4(),
            call_ref=call_ref,
            purpose_code=purpose_code,
            context_value_band=context_value_band,
            owner_sub=owner_sub,
            tenant_id=tenant_id,
            created_at=now,
            expires_at=now + timedelta(seconds=self._ttl),
            consent_acknowledged=consent_acknowledged,
        )
        self._records[record.session_id] = record
        return record

    def get(self, session_id: str | UUID, *, owner_sub: str) -> SessionRecord:
        """Look up a session owned by this principal.

        Raises:
            SessionError: if it is unknown, expired, or owned by someone else.
        """
        try:
            key = session_id if isinstance(session_id, UUID) else UUID(str(session_id))
        except ValueError as exc:
            raise SessionError("unknown session") from exc

        record = self._records.get(key)
        now = self._clock()
        if record is None or record.is_expired(now) or record.owner_sub != owner_sub:
            raise SessionError("unknown session")
        return record

    def begin_stream(self, record: SessionRecord) -> None:
        """Claim the single stream slot for this session.

        One live stream per session. Two concurrent streams would interleave into one evidence
        sequence and one hash chain, making the audit trail describe a call that never happened.
        """
        if record.streaming:
            raise SessionAlreadyStreaming()
        record.streaming = True

    def end_stream(self, record: SessionRecord) -> None:
        """Release the stream slot. Idempotent; safe from a ``finally`` block."""
        record.streaming = False

    def discard(self, session_id: UUID) -> None:
        self._records.pop(session_id, None)

    def _expire(self, now: datetime) -> None:
        for key in [k for k, r in self._records.items() if r.is_expired(now) and not r.streaming]:
            del self._records[key]

    def __len__(self) -> int:
        return len(self._records)
