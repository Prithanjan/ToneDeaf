"""Audit persistence — the only writer to ``audit_event``.

Everything that makes a row defensible is computed in :mod:`app.audit.chain` (pure) and merely
*stored* here. This module holds the connection pool and the per-session sequence, and nothing else.

Two properties the implementation is shaped around:

* **The chain is serialized per session.** ``event_seq`` and ``prev_event_hash`` are assigned under a
  per-session lock, because two concurrent writers would compute both from the same predecessor and
  produce a fork — which a verifier reports as tampering. A demo with four concurrent streams and one
  Gateway task is exactly the condition that surfaces it.
* **Insert failures are loud.** A dropped audit row is the failure mode that matters most on this
  project: the product claim is persistent evidence, and a silently missing row is a hole in the
  chain that shows up as a verification failure days later. So a write error propagates.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import UUID, uuid4

import asyncpg

from app.audit.chain import CHAIN_FIELDS, event_hash
from app.constants import GENESIS_PREV_HASH
from app.telemetry.logging import get_logger

_log = get_logger(__name__)

#: Column order for the INSERT. Derived from the canonical field set so the two cannot drift: adding
#: a chain field without adding a column (or the reverse) fails at import, not at 2 a.m.
_INSERT_COLUMNS: tuple[str, ...] = (
    "event_id",
    *CHAIN_FIELDS,
    "prev_event_hash",
    "event_hash",
    "retention_expires_at",
)

_INSERT_SQL = (
    f"INSERT INTO audit_event ({', '.join(_INSERT_COLUMNS)}) "
    f"VALUES ({', '.join(f'${i}' for i in range(1, len(_INSERT_COLUMNS) + 1))})"
)


@dataclass(slots=True)
class _SessionChain:
    """Per-session chain head. Lives only as long as the stream."""

    lock: asyncio.Lock
    next_seq: int = 0
    prev_hash: bytes = GENESIS_PREV_HASH


class AuditWriter:
    """Writes chained, feature-only audit events."""

    __slots__ = ("_pool", "_chain_key", "_retention_days", "_chains")

    def __init__(self, pool: asyncpg.Pool, *, chain_key: bytes, retention_days: int):
        self._pool = pool
        self._chain_key = chain_key
        self._retention_days = retention_days
        self._chains: dict[str, _SessionChain] = {}

    async def resume(self, session_id: str) -> None:
        """Load the chain head for a session that already has events.

        Needed because a reconnect creates a new stream object against an existing ``session_id``.
        Restarting from genesis would fork the chain; continuing from the stored head does not.
        """
        row = await self._pool.fetchrow(
            "SELECT event_seq, event_hash FROM audit_event "
            "WHERE session_id = $1 ORDER BY event_seq DESC LIMIT 1",
            UUID(session_id),
        )
        chain = self._chains.setdefault(session_id, _SessionChain(lock=asyncio.Lock()))
        if row is not None:
            chain.next_seq = int(row["event_seq"]) + 1
            chain.prev_hash = bytes(row["event_hash"])

    async def append(self, session_id: str, fields: Mapping[str, Any]) -> tuple[UUID, int]:
        """Chain and insert one event.

        ``fields`` must contain exactly the canonical field set minus ``event_seq`` (assigned here).
        :func:`app.audit.chain.canonicalize` rejects anything else, which is what stops an
        audio-adjacent field from being stored-but-unhashed.

        Returns:
            ``(event_id, event_seq)`` for echoing to the client in ``policy.action``.
        """
        chain = self._chains.setdefault(session_id, _SessionChain(lock=asyncio.Lock()))

        async with chain.lock:
            seq = chain.next_seq
            prev = chain.prev_hash

            event: dict[str, Any] = {**fields, "event_seq": seq}
            digest = event_hash(self._chain_key, event, prev)

            event_id = uuid4()
            occurred_at = event["occurred_at"]
            if not isinstance(occurred_at, datetime):
                raise TypeError("occurred_at must be a timezone-aware datetime")
            retention_expires_at = occurred_at + timedelta(days=self._retention_days)

            values = [
                event_id,
                *(_coerce(event[name]) for name in CHAIN_FIELDS),
                prev,
                digest,
                retention_expires_at,
            ]

            try:
                await self._pool.execute(_INSERT_SQL, *values)
            except asyncpg.PostgresError:
                # Do not advance the head on failure: the next event must chain from the last row
                # that actually exists, or the chain forks.
                _log.error(
                    "audit insert failed",
                    extra={"session_id": session_id, "event_seq": seq},
                    exc_info=True,
                )
                raise

            chain.next_seq = seq + 1
            chain.prev_hash = digest

        return event_id, seq

    def forget(self, session_id: str) -> None:
        """Drop the in-memory chain head when a stream ends. Idempotent."""
        self._chains.pop(session_id, None)

    async def verify_session(self, session_id: str) -> "tuple[bool, int | None]":
        """Recompute the chain for one session from the stored rows.

        Backs ``GET /api/v1/sessions/{id}/audit`` and the Phase-1 tamper test. Reads the columns
        explicitly rather than ``SELECT *`` for the same reason the chain field list is explicit
        (decision D-9).
        """
        columns = ", ".join((*CHAIN_FIELDS, "prev_event_hash", "event_hash"))
        rows = await self._pool.fetch(
            f"SELECT {columns} FROM audit_event WHERE session_id = $1 ORDER BY event_seq ASC",
            UUID(session_id),
        )

        from app.audit.chain import verify_chain  # local import keeps the pure module import-light

        result = verify_chain(self._chain_key, [dict(r) for r in rows])
        return result.ok, result.first_bad_event_seq

    async def delete_expired(self, *, now: datetime | None = None) -> int:
        """Whole-session atomic retention deletion (BUG-13). Returns rows deleted.

        A session is deleted only when all of its rows have expired. Deleting an individual row
        from a session breaks the survivor rows' hash chain, forging a false tamper signal.
        """
        cutoff = now or datetime.now(tz=timezone.utc)
        query = """
            WITH expired_sessions AS (
                SELECT session_id
                FROM audit_event
                GROUP BY session_id
                HAVING MAX(retention_expires_at) <= $1
            ),
            gone AS (
                DELETE FROM audit_event
                WHERE session_id IN (SELECT session_id FROM expired_sessions)
                RETURNING 1
            )
            SELECT count(*) FROM gone
        """
        deleted = await self._pool.fetchval(query, cutoff)
        return int(deleted or 0)


def _coerce(value: Any) -> Any:
    """Map Python values to what asyncpg expects for the declared column types."""
    if isinstance(value, str) and len(value) == 36 and value.count("-") == 4:
        try:
            return UUID(value)
        except ValueError:
            return value
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return value
