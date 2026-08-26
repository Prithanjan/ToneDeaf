"""Audit retention worker — deletes expired evidence WITHOUT breaking the hash chain.

Run it:

    DATABASE_URL=postgresql://... python audit/retention_worker.py --retention-days 7
    DATABASE_URL=postgresql://... python audit/retention_worker.py --dry-run

--------------------------------------------------------------------------------------------------
The decision this module exists to make
--------------------------------------------------------------------------------------------------

Deleting an audit row breaks the chain for its session. ``gateway/tests/test_audit_chain.py::
TestVerification::test_deleted_row_is_caught`` asserts exactly that, on purpose: deletion is the
tampering a per-row signature would miss, and the chain catches it because the successor's stored
``prev_event_hash`` no longer resolves. A retention worker that issues a naive
``DELETE FROM audit_event WHERE retention_expires_at <= now()`` therefore does not "expire old
evidence" — it *forges a tamper signal* on every session it touches, and the tamper signal it forges
is indistinguishable from the one an attacker leaves behind. That defeats the entire audit claim
(prd.md §6, threat "Audit record alteration").

**Chosen: whole-session atomic deletion.** A session is deleted only when *every* row in it has
expired, and then all of its rows go in one statement, in one transaction. The invariant a verifier
gets to rely on is:

    for every session present in audit_event: its rows are contiguous from event_seq 0 and the chain
    verifies from GENESIS_PREV_HASH to its head.

Whole-session deletion preserves that with no change to the verifier and no new trusted metadata. A
session that has been retained out of existence is simply absent, and absence of a session is not a
chain property — there is nothing for a verifier to fail on.

Note that "delete the expired prefix of a session" does NOT work, even though ``retention_expires_at``
is excluded from the hash input. Exclusion means a retention *timestamp edit* does not invalidate
history (``test_retention_edit_does_not_break_verification``); it does not mean rows are removable.
``verify_chain`` starts from ``GENESIS_PREV_HASH``, so after a prefix delete the surviving first row
carries its deleted predecessor's hash in ``prev_event_hash`` and verification fails at index 0 with
"prev_event_hash does not match the recomputed chain" — which is also what a wrong chain key and a
truncation attack look like. ``AuditWriter.delete_expired`` in ``gateway/app/audit/writer.py`` does
the naive delete and its docstring claims the chain "simply starts later"; that claim is wrong, this
module supersedes it, and the divergence is named in ``audit/README.md`` (rules.md R-54).

**Rejected: a tombstone the verifier understands.** For a verifier to resume across a gap, the
tombstone has to carry the deleted predecessor's ``event_hash`` so the chain can be re-anchored. That
value is already sitting in the surviving successor's ``prev_event_hash`` column, readable by anyone
with SELECT. So anyone with write access could delete the rows they want to hide and write a tombstone
that "explains" the truncation using data they just read — converting a *detectable* chain break into
an authorized-looking gap. The tombstone would hand an attacker a laundering primitive for precisely
the tampering the chain exists to detect. Making the tombstone itself HMAC-authenticated closes the
forgery, but then it is a chain event, so it belongs in ``CHAIN_FIELDS`` — a ``CHAIN_FIELD_SET_VERSION``
bump and a documented re-anchor that invalidates every historical hash (rules.md R-27), spent on a
retention convenience.

**Also rejected: delete the prefix and re-chain the survivors.** Recomputing hashes for rows that
remain is rewriting the evidence. It is the one operation the chain exists to make impossible, and a
process that does it routinely cannot be told apart from an attacker doing it once.

The cost of the chosen design, stated rather than hidden: a session's earliest event lives until the
session's *latest* event expires, so it outlives its nominal retention by up to the session's
duration. Sessions here are minutes and retention is days, so the overshoot is small — but it is
measured, not assumed, and reported in every receipt as ``max_retention_overshoot_seconds``.

--------------------------------------------------------------------------------------------------
Safe to run twice
--------------------------------------------------------------------------------------------------

* Idempotent by construction: a second run finds no fully-expired sessions and deletes nothing.
* Concurrency-safe: a session-level PostgreSQL advisory lock means a second *simultaneous* run exits
  cleanly with ``lock_acquired: false`` rather than racing. Two racing runs would both be correct
  (the DELETE re-asserts its own precondition) but would double-count in their receipts, and a
  retention report that overstates what it deleted is not evidence.
* The DELETE re-checks "no row of this session survives the cutoff" inside the statement, so a session
  that is resumed between the plan and the delete is skipped whole rather than half-deleted.

The receipt carries counts, UUID session identifiers, and timing — no ``call_ref``, no
``purpose_code``, no risk value, no free text. See :data:`RECEIPT_FIELDS`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Final, Protocol, Sequence
from uuid import UUID, uuid4

TABLE_NAME: Final[str] = "audit_event"

#: Session-level advisory lock key. Derived deterministically from a namespace string so both tiers
#: and every operator compute the same value without a shared constant file:
#: ``int.from_bytes(sha256(b"sih26104.audit_retention").digest()[:8], "big", signed=True)``.
ADVISORY_LOCK_KEY: Final[int] = int.from_bytes(
    sha256(b"sih26104.audit_retention").digest()[:8], "big", signed=True
)

#: Receipts are logged, and a log line carrying ten thousand UUIDs is a log line nobody reads. The
#: full set is still accounted for: ``deleted_session_digest`` covers all of it and
#: ``session_ids_omitted`` says how many were left out (rules.md R-52 — never silently truncate).
MAX_RECEIPT_SESSION_IDS: Final[int] = 50

#: Default cap on sessions per run, so a first sweep over a neglected table cannot hold one
#: transaction open across the whole table. Each session is its own transaction regardless; this
#: bounds the run, not the atomicity.
DEFAULT_SESSION_LIMIT: Final[int] = 5_000


# --------------------------------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------------------------------

#: "Which sessions have every row expired?" — ``HAVING max(retention_expires_at) <= cutoff`` is the
#: whole-session predicate written as one aggregate. It also excludes live sessions for free: a
#: session being streamed right now has just-written rows whose expiry is days away.
#:
#: Supported by ``ix_audit_event_session_retention (session_id, retention_expires_at)``, which lets
#: PostgreSQL answer this as an index-only GroupAggregate rather than a heap scan plus sort.
SELECT_FULLY_EXPIRED_SESSIONS: Final[str] = f"""
SELECT session_id,
       count(*)::bigint          AS event_count,
       min(event_seq)::bigint    AS min_event_seq,
       max(event_seq)::bigint    AS max_event_seq,
       min(retention_expires_at) AS first_expiry,
       max(retention_expires_at) AS last_expiry
FROM {TABLE_NAME}
GROUP BY session_id
HAVING max(retention_expires_at) <= $1
ORDER BY max(retention_expires_at)
LIMIT $2
"""

#: Cheap "is there anything to do at all?" probe against
#: ``ix_audit_event_retention_expires_at``, so an hourly worker on an idle demo database costs one
#: index lookup instead of a full aggregate.
PROBE_ANY_EXPIRED: Final[str] = f"""
SELECT EXISTS (SELECT 1 FROM {TABLE_NAME} WHERE retention_expires_at <= $1)
"""

#: The atomic whole-session delete.
#:
#: The ``NOT EXISTS`` clause is the load-bearing line in this file. It re-asserts the whole-session
#: precondition *inside the statement*, so a session resumed between the plan and the delete is
#: skipped entirely instead of losing its expired prefix — which would be the exact chain break this
#: module exists to avoid. The subquery is uncorrelated with the outer row, so PostgreSQL evaluates it
#: once as an InitPlan; it is a guard, not a per-row cost.
DELETE_WHOLE_SESSION: Final[str] = f"""
WITH doomed AS (
    DELETE FROM {TABLE_NAME}
     WHERE session_id = $1
       AND NOT EXISTS (
           SELECT 1 FROM {TABLE_NAME} survivor
            WHERE survivor.session_id = $1
              AND survivor.retention_expires_at > $2
       )
    RETURNING 1
)
SELECT count(*)::bigint FROM doomed
"""

#: Post-delete assertion, run in the SAME transaction. If a session is not empty after its delete
#: reported rows removed, the transaction is rolled back: a partially deleted session must never
#: become durable, because that is a permanently unverifiable chain.
COUNT_SESSION_ROWS: Final[str] = f"SELECT count(*)::bigint FROM {TABLE_NAME} WHERE session_id = $1"

TRY_ADVISORY_LOCK: Final[str] = "SELECT pg_try_advisory_lock($1)"
ADVISORY_UNLOCK: Final[str] = "SELECT pg_advisory_unlock($1)"


class RetentionError(RuntimeError):
    """The sweep could not be completed safely. Nothing partial was committed."""


# --------------------------------------------------------------------------------------------------
# Pure planning
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """How long evidence lives. Pure; holds no clock and no connection (rules.md R-53 in spirit).

    ``retention_days`` mirrors ``gateway/app/config.py::Settings.audit_retention_days`` (default 7).
    It is passed in rather than imported because importing the Gateway's pydantic settings would drag
    a required-secrets validator into a maintenance job that has no business holding secrets.
    """

    retention_days: int
    session_limit: int = DEFAULT_SESSION_LIMIT

    def __post_init__(self) -> None:
        if self.retention_days < 1:
            # Zero would delete a session the moment it closed, which is not a retention policy but a
            # deletion policy — and it would delete evidence of the demo currently being judged.
            raise ValueError("retention_days must be >= 1")
        if self.session_limit < 1:
            raise ValueError("session_limit must be >= 1")

    def cutoff(self, now: datetime) -> datetime:
        """Rows are expired when ``retention_expires_at <= cutoff``.

        The Gateway already stamped ``retention_expires_at = occurred_at + retention_days`` at insert
        time, so the cutoff is simply *now*: recomputing ``now - retention_days`` here would apply
        today's policy to yesterday's rows and silently change the meaning of a stored expiry. The
        policy is carried in the row, which is why a retention change cannot rewrite history.

        ``retention_days`` is still taken and validated because it is reported in the receipt: a
        receipt that does not say which policy produced it is not evidence.
        """
        if now.tzinfo is None:
            # A naive datetime here would compare against timestamptz by assuming the server's zone,
            # so a sweep run in IST would delete up to 5.5 hours of not-yet-expired evidence.
            raise ValueError("now must be timezone-aware")
        return now.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class SessionPlan:
    """One fully-expired session, as the planner sees it. No caller reference, by construction."""

    session_id: UUID
    event_count: int
    min_event_seq: int
    max_event_seq: int
    first_expiry: datetime
    last_expiry: datetime

    @property
    def overshoot_seconds(self) -> int:
        """How much longer the session's EARLIEST event lived than the policy nominally allowed.

        This is the measured price of whole-session deletion (see the module docstring). Reporting it
        is what keeps the design choice honest: if this number ever approaches the retention period,
        the assumption "sessions are short" has stopped holding and the decision needs revisiting.
        """
        return int((self.last_expiry - self.first_expiry).total_seconds())

    @property
    def is_contiguous(self) -> bool:
        """Whether ``event_seq`` runs 0..n-1 with no gaps.

        Not a retention precondition — it is a *finding*. A non-contiguous session is already an
        unverifiable chain, so it is reported rather than quietly deleted: deleting it would destroy
        the only evidence that something went wrong.
        """
        return self.min_event_seq == 0 and self.max_event_seq == self.event_count - 1


# --------------------------------------------------------------------------------------------------
# The receipt
# --------------------------------------------------------------------------------------------------

#: Every field a receipt may contain. The structural guarantee is that :meth:`RetentionReceipt.to_dict`
#: serializes exactly this tuple, and an import-time assertion below pins it to the dataclass's fields
#: — so a field cannot be added to the receipt without appearing here, where it is reviewed.
#:
#: ``audit/tests/test_retention_worker.py`` runs this tuple through
#: ``schema_contract.forbidden_substring_hits``, i.e. the same §5.2 deny-list vocabulary applied to
#: database columns. The receipt is durable output, so the same privacy rule applies to it (R-14).
#: The deny-list is not duplicated here; the test owns the cross-check and ``schema_contract`` owns
#: the list.
#:
#: ``session_ids`` is included deliberately. A ``session_id`` is a random UUID surrogate — it is
#: already in the redacting logger's ``ALLOWED_EXTRA_KEYS`` and in API paths, and once the rows are
#: gone there is nothing left for it to link to. Without it a receipt cannot answer "did we delete the
#: right thing", which is the question the Data gate asks.
RECEIPT_FIELDS: Final[tuple[str, ...]] = (
    "run_id",
    "started_at",
    "finished_at",
    "cutoff",
    "retention_days",
    "dry_run",
    "lock_acquired",
    "sessions_examined",
    "sessions_deleted",
    "sessions_skipped_reappeared",
    "sessions_skipped_non_contiguous",
    "events_deleted",
    "max_retention_overshoot_seconds",
    "session_ids",
    "session_ids_omitted",
    "deleted_session_digest",
)


@dataclass(frozen=True, slots=True)
class RetentionReceipt:
    """What one sweep did, in a form that is safe to log, commit, and hand to a judge.

    Deliberately not a database table. ``technical-design.md`` §5.1 is titled "the complete list", and
    the deny-list test asserts the audit schema's tables and columns as an exact set; adding an
    ``audit_retention_run`` table would mean extending §5.1 to record housekeeping. Receipts are
    operational evidence, not audit evidence, so they belong outside the evidence table.

    A receipt is explicitly NOT an input to chain verification. If a verifier consulted it to decide
    which absences were legitimate, the receipt would become a forgeable way to explain a gap — the
    same defect that ruled out tombstones. Whole-session deletion needs no such input.
    """

    run_id: str
    started_at: str
    finished_at: str
    cutoff: str
    retention_days: int
    dry_run: bool
    lock_acquired: bool
    sessions_examined: int
    sessions_deleted: int
    sessions_skipped_reappeared: int
    sessions_skipped_non_contiguous: int
    events_deleted: int
    max_retention_overshoot_seconds: int
    session_ids: tuple[str, ...]
    session_ids_omitted: int
    deleted_session_digest: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize exactly :data:`RECEIPT_FIELDS` — an allow-list, not ``asdict``.

        ``dataclasses.asdict`` would emit whatever fields exist, which is how a debugging field ends
        up in a durable receipt. This drops anything not reviewed.
        """
        return {name: getattr(self, name) for name in RECEIPT_FIELDS}

    def to_json(self) -> str:
        """Canonical JSON: sorted keys, compact separators — the same discipline as the hash chain, so
        two receipts for two tiers can be diffed byte-for-byte."""
        payload = self.to_dict()
        payload["session_ids"] = list(payload["session_ids"])
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def digest_of(session_ids: Sequence[str]) -> str:
    """SHA-256 over the sorted, newline-joined session id set.

    Lets two parties agree on *which* sessions a sweep removed without either of them having to hold
    the list, and lets a truncated ``session_ids`` field still be accounted for.
    """
    joined = "\n".join(sorted(session_ids)).encode("utf-8")
    return sha256(joined).hexdigest()


def build_receipt(
    *,
    run_id: str,
    started_at: datetime,
    finished_at: datetime,
    cutoff: datetime,
    policy: RetentionPolicy,
    dry_run: bool,
    lock_acquired: bool,
    sessions_examined: int,
    deleted: Sequence[SessionPlan],
    skipped_reappeared: int,
    skipped_non_contiguous: int,
) -> RetentionReceipt:
    """Assemble a receipt. Pure — no clock read, no I/O — so it is directly unit-testable."""
    ids = [str(plan.session_id) for plan in deleted]
    shown = sorted(ids)[:MAX_RECEIPT_SESSION_IDS]
    return RetentionReceipt(
        run_id=run_id,
        started_at=_rfc3339(started_at),
        finished_at=_rfc3339(finished_at),
        cutoff=_rfc3339(cutoff),
        retention_days=policy.retention_days,
        dry_run=dry_run,
        lock_acquired=lock_acquired,
        sessions_examined=sessions_examined,
        sessions_deleted=len(deleted),
        sessions_skipped_reappeared=skipped_reappeared,
        sessions_skipped_non_contiguous=skipped_non_contiguous,
        events_deleted=sum(plan.event_count for plan in deleted),
        max_retention_overshoot_seconds=max((p.overshoot_seconds for p in deleted), default=0),
        session_ids=tuple(shown),
        session_ids_omitted=len(ids) - len(shown),
        deleted_session_digest=digest_of(ids),
    )


def _rfc3339(value: datetime) -> str:
    """UTC, microseconds, ``Z`` — the same timestamp format the hash chain canonicalizes to."""
    if value.tzinfo is None:
        raise ValueError("receipt timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


# --------------------------------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------------------------------


class Connection(Protocol):
    """The slice of ``asyncpg.Connection`` this module uses.

    Structural typing rather than an ``asyncpg`` import at module scope, so the pure parts above can be
    imported and tested with nothing installed. ``asyncpg`` is imported inside :func:`main`.
    """

    async def fetch(self, query: str, *args: Any) -> Sequence[Any]: ...
    async def fetchval(self, query: str, *args: Any) -> Any: ...
    def transaction(self) -> Any: ...


async def plan_sweep(
    conn: Connection, policy: RetentionPolicy, cutoff: datetime
) -> list[SessionPlan]:
    """Find fully-expired sessions. Read-only; a dry run stops after this."""
    if not await conn.fetchval(PROBE_ANY_EXPIRED, cutoff):
        return []
    rows = await conn.fetch(SELECT_FULLY_EXPIRED_SESSIONS, cutoff, policy.session_limit)
    return [
        SessionPlan(
            session_id=row["session_id"],
            event_count=int(row["event_count"]),
            min_event_seq=int(row["min_event_seq"]),
            max_event_seq=int(row["max_event_seq"]),
            first_expiry=row["first_expiry"],
            last_expiry=row["last_expiry"],
        )
        for row in rows
    ]


async def delete_session(conn: Connection, plan: SessionPlan, cutoff: datetime) -> int:
    """Delete one whole session atomically. Returns rows removed; 0 means "skipped, unchanged".

    Raises:
        RetentionError: if the session is not empty afterwards. The surrounding transaction is rolled
            back by the ``async with`` on the way out, so a half-deleted session cannot become
            durable. This is belt-and-braces over the ``NOT EXISTS`` guard in the statement — the
            failure it protects against (a permanently unverifiable chain) is not recoverable, so it
            is worth one extra ``count(*)``.
    """
    async with conn.transaction():
        removed = int(await conn.fetchval(DELETE_WHOLE_SESSION, plan.session_id, cutoff) or 0)
        if removed == 0:
            # The NOT EXISTS guard fired: the session was resumed and now has unexpired rows. Correct
            # outcome, not an error — it will be swept once those rows expire too.
            return 0
        remaining = int(await conn.fetchval(COUNT_SESSION_ROWS, plan.session_id) or 0)
        if remaining != 0:
            raise RetentionError(
                f"refusing to commit a partially deleted session: {remaining} row(s) remain after "
                f"deleting {removed}. A partial session is an unverifiable chain (R-14 evidence "
                f"integrity); rolling back."
            )
    return removed


async def run_sweep(
    conn: Connection,
    policy: RetentionPolicy,
    *,
    now: datetime,
    dry_run: bool = False,
) -> RetentionReceipt:
    """One complete sweep. Returns the receipt; never raises for "nothing to do"."""
    run_id = uuid4().hex
    started_at = now
    cutoff = policy.cutoff(now)

    locked = bool(await conn.fetchval(TRY_ADVISORY_LOCK, ADVISORY_LOCK_KEY))
    if not locked:
        # Another sweep holds the lock. Exiting cleanly is the point of "safe to run twice": a cron
        # overlap must not become an alert, and it must not produce a second receipt claiming the same
        # deletions.
        return build_receipt(
            run_id=run_id,
            started_at=started_at,
            finished_at=started_at,
            cutoff=cutoff,
            policy=policy,
            dry_run=dry_run,
            lock_acquired=False,
            sessions_examined=0,
            deleted=(),
            skipped_reappeared=0,
            skipped_non_contiguous=0,
        )

    deleted: list[SessionPlan] = []
    plans: list[SessionPlan] = []
    skipped_reappeared = 0
    skipped_non_contiguous = 0
    try:
        plans = await plan_sweep(conn, policy, cutoff)
        for plan in plans:
            if not plan.is_contiguous:
                # Already unverifiable. Deleting it would destroy the only evidence that a chain was
                # broken, so it stays and is reported. An operator decides, not a cron job.
                skipped_non_contiguous += 1
                continue
            if dry_run:
                deleted.append(plan)
                continue
            if await delete_session(conn, plan, cutoff) == 0:
                skipped_reappeared += 1
            else:
                deleted.append(plan)
    finally:
        await conn.fetchval(ADVISORY_UNLOCK, ADVISORY_LOCK_KEY)

    return build_receipt(
        run_id=run_id,
        started_at=started_at,
        finished_at=datetime.now(tz=timezone.utc),
        cutoff=cutoff,
        policy=policy,
        dry_run=dry_run,
        lock_acquired=True,
        sessions_examined=len(plans),
        deleted=deleted,
        skipped_reappeared=skipped_reappeared,
        skipped_non_contiguous=skipped_non_contiguous,
    )


# --------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------

EXIT_OK: Final[int] = 0
EXIT_ERROR: Final[int] = 1
#: Distinct from EXIT_ERROR so a scheduler can treat "a partial session was refused" as the page-worthy
#: case and everything else as noise.
EXIT_UNSAFE: Final[int] = 4


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete fully-expired audit sessions without breaking the hash chain.",
        epilog="Deletes WHOLE SESSIONS only. See the module docstring for why a partial delete is a "
        "forged tamper signal.",
    )
    parser.add_argument("--retention-days", type=int, default=int(os.environ.get("AUDIT_RETENTION_DAYS", "7")))
    parser.add_argument("--session-limit", type=int, default=DEFAULT_SESSION_LIMIT)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only. Reports what WOULD be deleted and deletes nothing.",
    )
    return parser.parse_args(argv)


async def _amain(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return EXIT_ERROR

    import asyncpg  # imported here so the pure parts above stay importable without a driver

    policy = RetentionPolicy(retention_days=args.retention_days, session_limit=args.session_limit)
    # asyncpg does not accept driver suffixes (+asyncpg, +psycopg2, etc.); normalize to postgresql://
    clean_url = re.sub(r"^postgres(?:ql)?(?:\+[a-zA-Z0-9_]+)?://", "postgresql://", url)
    conn = await asyncpg.connect(clean_url)
    try:
        receipt = await run_sweep(conn, policy, now=datetime.now(tz=timezone.utc), dry_run=args.dry_run)
    except RetentionError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_UNSAFE
    finally:
        await conn.close()

    # stdout, one line, canonical JSON: greppable, diffable, and free of personal data by construction.
    print(receipt.to_json())
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


def _self_check() -> None:
    """The receipt's field allow-list must match the dataclass exactly.

    Without this, adding a field to :class:`RetentionReceipt` would silently omit it from
    :meth:`RetentionReceipt.to_dict` (data lost from a report, R-52) and adding a name to
    :data:`RECEIPT_FIELDS` without the field would raise ``AttributeError`` at report time — during a
    scheduled run, where nobody is watching. Both become import failures instead.
    """
    declared = tuple(f.name for f in fields(RetentionReceipt))
    assert declared == RECEIPT_FIELDS, (
        "RECEIPT_FIELDS and RetentionReceipt have diverged: "
        f"{sorted(set(declared) ^ set(RECEIPT_FIELDS))}"
    )


_self_check()


if __name__ == "__main__":
    raise SystemExit(main())
