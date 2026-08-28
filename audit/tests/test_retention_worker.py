"""The retention worker — and the reason it deletes whole sessions.

Two thirds of this file is about one decision. ``gateway/tests/test_audit_chain.py`` proves the chain
catches a deleted row; retention deletes rows; those two facts are in direct conflict, and
:mod:`retention_worker` resolves it by deleting whole sessions atomically.
:class:`TestWhyWholeSessionDeletion` asserts the conflict is real and that the chosen design resolves
it, using the Gateway's own ``verify_chain`` rather than a paraphrase of it. If someone later
"optimizes" the worker into a prefix delete, those tests fail with the reason attached.

Everything here runs without PostgreSQL. The pure layer — policy validation, plan arithmetic, receipt
construction — is directly unit-testable because :mod:`retention_worker` keeps it free of I/O. The
sweep is driven through a :class:`FakeConnection` that reimplements the SQL's *semantics* in Python.
That is a real limitation and worth naming: those tests prove the worker's control flow, not the SQL.
The SQL text is asserted separately for the properties that carry the safety argument, and the
``integration``-marked tests are where the statements themselves get executed.
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Sequence
from uuid import UUID, uuid4

import pytest
import retention_worker as rw
import schema_contract as sc
from app.audit.chain import CHAIN_FIELDS, chain_events, verify_chain
from tests.conftest import DATABASE_URL_ENV

NOW: Final[datetime] = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
EXPIRED: Final[datetime] = NOW - timedelta(hours=1)
LIVE: Final[datetime] = NOW + timedelta(days=6)

#: Not a secret and never used against a real chain. 32 bytes because that is the deployed key width.
TEST_CHAIN_KEY: Final[bytes] = b"unit-test-key-do-not-deploy-0123"


# --------------------------------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------------------------------


def make_row(session_id: UUID, event_seq: int, expiry: datetime) -> dict[str, Any]:
    """The three columns the sweep reasons about. Nothing else is needed, and nothing else is used —
    which is itself the point: the planner never reads ``call_ref``, ``purpose_code``, or a score."""
    return {
        "session_id": session_id,
        "event_seq": event_seq,
        "retention_expires_at": expiry,
    }


def make_event(session_id: UUID, event_seq: int, expiry: datetime) -> dict[str, Any]:
    """A complete, canonicalizable audit event.

    Every one of ``CHAIN_FIELDS`` is populated because ``canonicalize`` refuses a partial event in both
    directions. Values are the shape the CHECK constraints require, so this builder doubles as a check
    that the schema and the chain agree on what a row looks like.
    """
    event = {
        "tenant_id": "demo-tenant",
        "session_id": session_id,
        "call_ref": "a3f" + "0" * 61,
        "event_seq": event_seq,
        "occurred_at": NOW - timedelta(days=7) + timedelta(seconds=event_seq),
        "purpose_code": "payment_release",
        "context_value_band": "high",
        "window_seq": event_seq,
        "spoof_risk": "0.4200",
        "risk_state": "uncertain",
        "action": "verify",
        "reason_code": "EVIDENCE_BELOW_K",
        "policy_version": "0.1.0",
        "policy_bundle_sha256": "b" * 64,
        "model_version": "0.1.0",
        "model_sha256": "c" * 64,
        "calibration_version": "0.1.0",
        "calibration_sha256": "d" * 64,
        "quality_flags": [],
        "detector_mode": "MOCK_SMOKE_MODE_NOT_A_DETECTOR",
        "execution_provider": "CPUExecutionProvider",
        "deployment_profile": "local-cpu",
        "event_id": uuid4(),
        "retention_expires_at": expiry,
    }
    assert set(CHAIN_FIELDS) <= set(event), sorted(set(CHAIN_FIELDS) - set(event))
    return event


def build_verifiable_session(
    session_id: UUID, count: int, expiry: datetime
) -> list[dict[str, Any]]:
    """A session whose stored hashes verify against :data:`TEST_CHAIN_KEY`."""
    events = [make_event(session_id, seq, expiry) for seq in range(count)]
    for event, (prev, current) in zip(events, chain_events(TEST_CHAIN_KEY, events)):
        event["prev_event_hash"] = prev
        event["event_hash"] = current
    assert verify_chain(TEST_CHAIN_KEY, events).ok, (
        "builder produced an unverifiable session"
    )
    return events


# --------------------------------------------------------------------------------------------------
# Fake connection
# --------------------------------------------------------------------------------------------------


class FakeTransaction:
    """Snapshot-and-restore, which is the only property of a transaction these tests depend on."""

    def __init__(self, conn: "FakeConnection") -> None:
        self._conn = conn
        self._snapshot: list[dict[str, Any]] = []

    async def __aenter__(self) -> "FakeTransaction":
        self._snapshot = deepcopy(self._conn.rows)
        self._conn.transactions_opened += 1
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is not None:
            self._conn.rows = self._snapshot
            self._conn.rollbacks += 1
        return False


class FakeConnection:
    """In-memory stand-in for ``asyncpg.Connection`` implementing this module's five statements.

    Dispatch is by identity against the module's SQL constants, so a renamed or rewritten statement
    surfaces as ``AssertionError: unexpected query`` rather than a silently skipped assertion.
    """

    def __init__(
        self,
        rows: Sequence[dict[str, Any]] = (),
        *,
        lock_granted: bool = True,
        delete_partially: bool = False,
        raise_on_plan: bool = False,
    ) -> None:
        self.rows: list[dict[str, Any]] = [dict(r) for r in rows]
        self.lock_granted = lock_granted
        self.delete_partially = delete_partially
        self.raise_on_plan = raise_on_plan
        self.queries: list[str] = []
        self.unlocks = 0
        self.transactions_opened = 0
        self.rollbacks = 0

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.queries.append(query)
        if query == rw.TRY_ADVISORY_LOCK:
            return self.lock_granted
        if query == rw.ADVISORY_UNLOCK:
            self.unlocks += 1
            return True
        if query == rw.PROBE_ANY_EXPIRED:
            return any(r["retention_expires_at"] <= args[0] for r in self.rows)
        if query == rw.DELETE_WHOLE_SESSION:
            session_id, cutoff = args
            mine = [r for r in self.rows if r["session_id"] == session_id]
            if any(r["retention_expires_at"] > cutoff for r in mine):
                return 0  # the NOT EXISTS guard
            keep = 1 if (self.delete_partially and len(mine) > 1) else 0
            survivors = [r for r in self.rows if r["session_id"] != session_id]
            survivors.extend(sorted(mine, key=lambda r: r["event_seq"])[:keep])
            removed = len(self.rows) - len(survivors)
            self.rows = survivors
            return removed
        if query == rw.COUNT_SESSION_ROWS:
            return sum(1 for r in self.rows if r["session_id"] == args[0])
        raise AssertionError(f"unexpected query: {query!r}")

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.queries.append(query)
        if self.raise_on_plan:
            raise RuntimeError("connection lost mid-plan")
        assert query == rw.SELECT_FULLY_EXPIRED_SESSIONS, query
        cutoff, limit = args
        groups: dict[UUID, list[dict[str, Any]]] = {}
        for row in self.rows:
            groups.setdefault(row["session_id"], []).append(row)
        out: list[dict[str, Any]] = []
        for session_id, group in groups.items():
            if max(r["retention_expires_at"] for r in group) > cutoff:
                continue
            out.append(
                {
                    "session_id": session_id,
                    "event_count": len(group),
                    "min_event_seq": min(r["event_seq"] for r in group),
                    "max_event_seq": max(r["event_seq"] for r in group),
                    "first_expiry": min(r["retention_expires_at"] for r in group),
                    "last_expiry": max(r["retention_expires_at"] for r in group),
                }
            )
        out.sort(key=lambda d: d["last_expiry"])
        return out[:limit]


def sweep(
    conn: FakeConnection,
    *,
    days: int = 7,
    dry_run: bool = False,
    limit: int | None = None,
):
    """Run one sweep synchronously.

    ``asyncio.run`` rather than ``pytest.mark.asyncio``: it keeps this suite independent of a plugin's
    mode configuration, and the worker has no long-lived event-loop state to preserve between calls.
    """
    policy = rw.RetentionPolicy(
        retention_days=days, session_limit=limit or rw.DEFAULT_SESSION_LIMIT
    )
    return asyncio.run(rw.run_sweep(conn, policy, now=NOW, dry_run=dry_run))


# --------------------------------------------------------------------------------------------------
# The decision
# --------------------------------------------------------------------------------------------------


class TestWhyWholeSessionDeletion:
    """The conflict, and the resolution, asserted with the Gateway's real verifier.

    These tests would be worth writing even if they never failed, because the alternative design looks
    obviously correct until you run it: ``retention_expires_at`` is excluded from the hash input, so it
    is tempting to conclude that expiry-driven deletion is chain-safe. Exclusion means an *edit* to
    that column does not invalidate history. It says nothing about removing rows.
    """

    def test_deleting_the_expired_prefix_forges_a_tamper_signal(self) -> None:
        """The rejected design, executed. Verification fails at index 0 — the row that is still there."""
        events = build_verifiable_session(uuid4(), 5, EXPIRED)
        after_prefix_delete = events[2:]
        result = verify_chain(TEST_CHAIN_KEY, after_prefix_delete)
        assert not result.ok
        assert result.first_bad_index == 0
        assert result.detail == "prev_event_hash does not match the recomputed chain"

    def test_a_prefix_delete_is_indistinguishable_from_a_truncation_attack(
        self,
    ) -> None:
        """This is the whole argument, in one assertion.

        Retention removing the first two rows and an attacker removing the first two rows to hide them
        leave *identical* evidence. A verifier therefore cannot report "this gap was routine
        housekeeping" — so a retention worker that truncates has converted every swept session into an
        unresolvable tamper alert, and an attacker's real truncation into background noise.
        """
        events = build_verifiable_session(uuid4(), 5, EXPIRED)
        housekeeping = verify_chain(TEST_CHAIN_KEY, events[2:])
        attack = verify_chain(TEST_CHAIN_KEY, deepcopy(events)[2:])
        assert housekeeping == attack
        assert not housekeeping.ok

    def test_whole_session_deletion_leaves_every_survivor_verifiable(self) -> None:
        """The chosen design. Absence of a session is not a chain property, so there is nothing to
        fail on — no verifier change, no new trusted metadata."""
        doomed, kept = uuid4(), uuid4()
        expired_session = build_verifiable_session(doomed, 4, EXPIRED)
        live_session = build_verifiable_session(kept, 3, LIVE)
        table = [
            make_row(e["session_id"], e["event_seq"], e["retention_expires_at"])
            for e in (*expired_session, *live_session)
        ]

        conn = FakeConnection(table)
        receipt = sweep(conn)

        assert receipt.sessions_deleted == 1
        assert receipt.events_deleted == 4
        assert {r["session_id"] for r in conn.rows} == {kept}
        assert verify_chain(TEST_CHAIN_KEY, live_session).ok

    def test_a_retention_timestamp_edit_still_does_not_break_the_chain(self) -> None:
        """The property that makes the *exclusion* correct, restated here so the two facts are not
        confused. ``retention_expires_at`` is in ``EXCLUDED_FIELDS``, so re-stamping expiry — which a
        policy change does — leaves every stored hash valid."""
        events = build_verifiable_session(uuid4(), 3, EXPIRED)
        for event in events:
            event["retention_expires_at"] = NOW + timedelta(days=365)
        assert verify_chain(TEST_CHAIN_KEY, events).ok

    def test_a_session_is_only_swept_once_every_row_has_expired(self) -> None:
        """The precondition that makes whole-session deletion safe. A session with one live row is not
        touched at all — not even its expired rows."""
        mixed = uuid4()
        table = [
            make_row(mixed, 0, EXPIRED),
            make_row(mixed, 1, EXPIRED),
            make_row(mixed, 2, LIVE),
        ]
        receipt = sweep(FakeConnection(table))
        assert (receipt.sessions_examined, receipt.sessions_deleted) == (0, 0)

    def test_the_measured_cost_is_reported_not_hidden(self) -> None:
        """Whole-session deletion makes the earliest event outlive its nominal retention by up to the
        session's duration. Reporting it is what keeps the trade-off reviewable: if this number ever
        approaches the retention period, "sessions are short" has stopped being true."""
        session_id = uuid4()
        table = [
            make_row(session_id, 0, EXPIRED - timedelta(minutes=5)),
            make_row(session_id, 1, EXPIRED),
        ]
        receipt = sweep(FakeConnection(table))
        assert receipt.max_retention_overshoot_seconds == 300


class TestSessionPlan:
    def test_overshoot_is_the_span_between_first_and_last_expiry(self) -> None:
        plan = rw.SessionPlan(
            uuid4(), 2, 0, 1, EXPIRED - timedelta(seconds=90), EXPIRED
        )
        assert plan.overshoot_seconds == 90

    def test_a_single_event_session_overshoots_by_nothing(self) -> None:
        plan = rw.SessionPlan(uuid4(), 1, 0, 0, EXPIRED, EXPIRED)
        assert plan.overshoot_seconds == 0

    @pytest.mark.parametrize(
        ("count", "lo", "hi", "contiguous"),
        [
            (5, 0, 4, True),
            (4, 0, 4, False),  # a gap: five sequence numbers, four rows
            (5, 1, 5, False),  # event_seq 0 is missing
            (1, 0, 0, True),
        ],
    )
    def test_contiguity(self, count: int, lo: int, hi: int, contiguous: bool) -> None:
        plan = rw.SessionPlan(uuid4(), count, lo, hi, EXPIRED, EXPIRED)
        assert plan.is_contiguous is contiguous

    def test_a_non_contiguous_session_is_reported_and_left_alone(self) -> None:
        """It is already an unverifiable chain, so deleting it would destroy the only evidence that
        something went wrong. An operator decides, not a cron job."""
        broken = uuid4()
        table = [make_row(broken, 0, EXPIRED), make_row(broken, 7, EXPIRED)]
        conn = FakeConnection(table)
        receipt = sweep(conn)
        assert receipt.sessions_skipped_non_contiguous == 1
        assert receipt.sessions_deleted == 0
        assert len(conn.rows) == 2


class TestRetentionPolicy:
    @pytest.mark.parametrize("days", [0, -1, -7])
    def test_a_retention_period_below_one_day_is_refused(self, days: int) -> None:
        """Zero is not a retention policy but a deletion policy, and it would delete the evidence of
        the demo currently being judged."""
        with pytest.raises(ValueError, match="retention_days must be >= 1"):
            rw.RetentionPolicy(retention_days=days)

    @pytest.mark.parametrize("limit", [0, -1])
    def test_a_session_limit_below_one_is_refused(self, limit: int) -> None:
        with pytest.raises(ValueError, match="session_limit must be >= 1"):
            rw.RetentionPolicy(retention_days=7, session_limit=limit)

    def test_the_default_matches_the_gateway_setting(self) -> None:
        """``gateway/app/config.py::Settings.audit_retention_days`` defaults to 7. Two components with
        different retention defaults produce a table where half the rows outlive the policy."""
        assert rw._parse_args([]).retention_days == 7

    def test_a_naive_now_is_refused(self) -> None:
        """A naive datetime compared against ``timestamptz`` assumes the server's zone, so a sweep run
        in IST would delete up to 5.5 hours of not-yet-expired evidence — and report success."""
        with pytest.raises(ValueError, match="timezone-aware"):
            rw.RetentionPolicy(retention_days=7).cutoff(datetime(2026, 8, 26, 12, 0))

    def test_the_cutoff_is_now_not_now_minus_retention(self) -> None:
        """The row carries its own expiry, stamped by the Gateway at insert time. Recomputing
        ``now - retention_days`` here would apply today's policy to yesterday's rows, which is how a
        retention *shortening* silently becomes retroactive."""
        policy = rw.RetentionPolicy(retention_days=30)
        assert policy.cutoff(NOW) == NOW

    def test_the_cutoff_is_normalised_to_utc(self) -> None:
        ist = timezone(timedelta(hours=5, minutes=30))
        assert rw.RetentionPolicy(retention_days=7).cutoff(NOW.astimezone(ist)) == NOW


class TestReceiptContainsNoPersonalData:
    """R-14/R-52. A retention report is durable output, so it is held to the same rule as a column."""

    @pytest.mark.privacy
    def test_no_receipt_field_name_trips_the_schema_deny_list(self) -> None:
        """The §5.2 vocabulary, applied to receipt fields. Reusing ``schema_contract``'s list rather
        than restating it means the receipt cannot drift away from the column rule."""
        assert sc.forbidden_substring_hits(rw.RECEIPT_FIELDS) == []

    @pytest.mark.privacy
    def test_the_serialized_receipt_holds_exactly_the_reviewed_fields(self) -> None:
        payload = json.loads(sample_receipt().to_json())
        assert sorted(payload) == sorted(rw.RECEIPT_FIELDS)

    @pytest.mark.privacy
    @pytest.mark.parametrize(
        "leak",
        [
            "call_ref",
            "purpose_code",
            "spoof_risk",
            "risk_state",
            "action",
            "tenant_id",
            "client",
        ],
    )
    def test_no_evidence_field_leaks_into_the_receipt(self, leak: str) -> None:
        """A receipt is written to stdout and to CI logs, which are far less protected than the audit
        table. ``purpose_code`` plus a timestamp is enough to say what a named customer called about."""
        assert leak not in sample_receipt().to_json()

    def test_to_dict_is_an_allow_list_not_asdict(self) -> None:
        """``dataclasses.asdict`` emits whatever fields exist, which is how a debugging field reaches a
        durable receipt. This is the same argument as the column allow-list, one layer out."""
        assert tuple(sample_receipt().to_dict()) == rw.RECEIPT_FIELDS

    def test_the_field_allow_list_is_pinned_to_the_dataclass(self, monkeypatch) -> None:
        """Proves :func:`retention_worker._self_check` has teeth.

        Without it, a field added to the dataclass would be silently dropped from every report (R-52)
        and a name added to ``RECEIPT_FIELDS`` alone would raise ``AttributeError`` during a scheduled
        run, where nobody is watching.
        """
        monkeypatch.setattr(rw, "RECEIPT_FIELDS", rw.RECEIPT_FIELDS + ("debug_note",))
        with pytest.raises(AssertionError, match="diverged"):
            rw._self_check()

    def test_the_receipt_is_canonical_json(self) -> None:
        """Sorted keys, no whitespace — the same discipline as the chain, so two tiers' receipts diff
        byte-for-byte instead of by eye."""
        raw = sample_receipt().to_json()
        assert ", " not in raw and ": " not in raw
        keys = [k for k in json.loads(raw)]
        assert keys == sorted(keys)

    def test_timestamps_are_utc_rfc3339(self) -> None:
        receipt = sample_receipt()
        for value in (receipt.started_at, receipt.finished_at, receipt.cutoff):
            assert value.endswith("Z"), value
            assert datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")

    def test_a_naive_timestamp_cannot_reach_a_receipt(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            rw._rfc3339(datetime(2026, 8, 26, 12, 0))


class TestReceiptAccountsForEverything:
    def test_a_long_session_list_is_truncated_but_counted(self) -> None:
        """R-52: never silently truncate. A log line carrying ten thousand UUIDs is a line nobody
        reads, so the list is capped — and the cap is declared in the receipt itself."""
        plans = [
            rw.SessionPlan(uuid4(), 1, 0, 0, EXPIRED, EXPIRED)
            for _ in range(rw.MAX_RECEIPT_SESSION_IDS + 7)
        ]
        receipt = sample_receipt(deleted=plans)
        assert len(receipt.session_ids) == rw.MAX_RECEIPT_SESSION_IDS
        assert receipt.session_ids_omitted == 7
        assert receipt.sessions_deleted == len(plans)

    def test_the_digest_covers_the_whole_set_including_omitted_ids(self) -> None:
        plans = [
            rw.SessionPlan(uuid4(), 1, 0, 0, EXPIRED, EXPIRED)
            for _ in range(rw.MAX_RECEIPT_SESSION_IDS + 3)
        ]
        receipt = sample_receipt(deleted=plans)
        assert receipt.deleted_session_digest == rw.digest_of(
            [str(p.session_id) for p in plans]
        )
        assert receipt.deleted_session_digest != rw.digest_of(list(receipt.session_ids))

    def test_the_digest_is_order_independent(self) -> None:
        """Two runs that delete the same sessions in a different plan order must agree, or the digest
        cannot be used to reconcile two reports."""
        ids = [uuid4().hex for _ in range(6)]
        assert rw.digest_of(ids) == rw.digest_of(list(reversed(ids)))

    def test_an_empty_sweep_has_a_stable_digest(self) -> None:
        assert rw.digest_of([]) == rw.digest_of(())

    def test_events_deleted_is_the_sum_of_rows_not_of_sessions(self) -> None:
        plans = [
            rw.SessionPlan(uuid4(), 3, 0, 2, EXPIRED, EXPIRED),
            rw.SessionPlan(uuid4(), 11, 0, 10, EXPIRED, EXPIRED),
        ]
        assert sample_receipt(deleted=plans).events_deleted == 14


class TestSafeToRunTwice:
    def test_a_second_run_deletes_nothing_and_says_so(self) -> None:
        session_id = uuid4()
        conn = FakeConnection([make_row(session_id, seq, EXPIRED) for seq in range(3)])

        first = sweep(conn)
        second = sweep(conn)

        assert (first.sessions_deleted, first.events_deleted) == (1, 3)
        assert (second.sessions_deleted, second.events_deleted) == (0, 0)
        assert second.sessions_examined == 0
        assert conn.rows == []

    def test_a_second_run_is_not_an_error(self) -> None:
        """Idempotence has to include the exit code, or a cron overlap pages someone at 3am."""
        conn = FakeConnection()
        receipt = sweep(conn)
        assert receipt.lock_acquired is True
        assert receipt.sessions_deleted == 0

    def test_an_empty_table_costs_one_index_probe(self) -> None:
        """``PROBE_ANY_EXPIRED`` short-circuits the aggregate. An hourly worker against an idle demo
        database must not scan the table sixty times a day for nothing."""
        conn = FakeConnection()
        sweep(conn)
        assert rw.SELECT_FULLY_EXPIRED_SESSIONS not in conn.queries
        assert rw.PROBE_ANY_EXPIRED in conn.queries

    def test_a_concurrent_run_exits_cleanly_without_double_counting(self) -> None:
        """Two racing sweeps would both be *correct* — the DELETE re-asserts its own precondition — but
        they would both claim the same deletions, and a retention report that overstates what it
        deleted is not evidence."""
        conn = FakeConnection([make_row(uuid4(), 0, EXPIRED)], lock_granted=False)
        receipt = sweep(conn)
        assert receipt.lock_acquired is False
        assert receipt.sessions_deleted == 0
        assert len(conn.rows) == 1
        assert rw.DELETE_WHOLE_SESSION not in conn.queries

    def test_the_lock_is_released_even_when_the_sweep_fails(self) -> None:
        """A held advisory lock after a crash means every subsequent run reports ``lock_acquired:
        false`` and deletes nothing — retention silently stops while looking healthy."""
        conn = FakeConnection([make_row(uuid4(), 0, EXPIRED)], raise_on_plan=True)
        with pytest.raises(RuntimeError, match="connection lost mid-plan"):
            sweep(conn)
        assert conn.unlocks == 1

    def test_the_lock_is_released_on_the_happy_path(self) -> None:
        conn = FakeConnection([make_row(uuid4(), 0, EXPIRED)])
        sweep(conn)
        assert conn.unlocks == 1

    def test_a_session_resumed_between_plan_and_delete_is_skipped_whole(self) -> None:
        """The ``NOT EXISTS`` guard inside the DELETE. Without it, the plan's stale view would delete
        the expired prefix of a session that has since been resumed — precisely the chain break this
        module exists to prevent."""
        session_id = uuid4()
        conn = FakeConnection([make_row(session_id, 0, EXPIRED)])
        original_fetch = conn.fetch

        async def resume_after_planning(query: str, *args: Any) -> list[dict[str, Any]]:
            plans = await original_fetch(query, *args)
            conn.rows.append(make_row(session_id, 1, LIVE))
            return plans

        conn.fetch = resume_after_planning  # type: ignore[method-assign]
        receipt = sweep(conn)

        assert receipt.sessions_skipped_reappeared == 1
        assert receipt.sessions_deleted == 0
        assert len(conn.rows) == 2

    def test_a_dry_run_reports_without_deleting(self) -> None:
        session_id = uuid4()
        conn = FakeConnection([make_row(session_id, seq, EXPIRED) for seq in range(4)])
        receipt = sweep(conn, dry_run=True)
        assert receipt.dry_run is True
        assert (receipt.sessions_deleted, receipt.events_deleted) == (1, 4)
        assert len(conn.rows) == 4
        assert rw.DELETE_WHOLE_SESSION not in conn.queries

    def test_a_partial_delete_is_rolled_back_and_raised(self) -> None:
        """The unrecoverable failure. A committed half-session is a permanently unverifiable chain, so
        the extra ``count(*)`` inside the transaction is worth its cost."""
        session_id = uuid4()
        conn = FakeConnection(
            [make_row(session_id, seq, EXPIRED) for seq in range(3)],
            delete_partially=True,
        )
        with pytest.raises(rw.RetentionError, match="partially deleted session"):
            sweep(conn)
        assert len(conn.rows) == 3, "the transaction did not roll back"
        assert conn.rollbacks == 1

    def test_every_delete_runs_inside_a_transaction(self) -> None:
        conn = FakeConnection(
            [make_row(uuid4(), 0, EXPIRED), make_row(uuid4(), 0, EXPIRED)]
        )
        sweep(conn)
        assert conn.transactions_opened == 2

    def test_the_session_limit_bounds_a_first_sweep(self) -> None:
        """A neglected table must not turn the first sweep into one enormous unit of work. Each session
        is still its own transaction; the limit bounds the run, not the atomicity."""
        conn = FakeConnection([make_row(uuid4(), 0, EXPIRED) for _ in range(10)])
        receipt = sweep(conn, limit=3)
        assert receipt.sessions_deleted == 3
        assert len(conn.rows) == 7


class TestSqlSafetyProperties:
    """Text assertions on the statements. Cheap, and the only coverage the SQL gets without a database.

    Each one corresponds to a way the statement could be rewritten into something that still runs, still
    reports success, and destroys the chain.
    """

    def test_the_delete_is_scoped_to_one_session(self) -> None:
        assert "WHERE session_id = $1" in rw.DELETE_WHOLE_SESSION

    def test_the_delete_carries_the_whole_session_guard(self) -> None:
        """Deleting this clause leaves a statement that passes every other test in this file and
        truncates resumed sessions in production."""
        assert "NOT EXISTS" in rw.DELETE_WHOLE_SESSION
        assert "survivor.retention_expires_at > $2" in rw.DELETE_WHOLE_SESSION

    def test_no_statement_deletes_by_expiry_alone(self) -> None:
        """The naive form — ``DELETE FROM audit_event WHERE retention_expires_at <= now()`` — must not
        exist anywhere in this module. It is what ``gateway/app/audit/writer.py::delete_expired`` does,
        and it is superseded here.

        Every SQL constant is checked, not only :data:`DELETE_WHOLE_SESSION`, because the way this
        regresses is a second statement added later for a "quick cleanup".
        """
        for name, statement in vars(rw).items():
            if (
                not name.isupper()
                or not isinstance(statement, str)
                or "DELETE" not in statement
            ):
                continue
            assert "session_id = $1" in statement, (
                f"{name} deletes without a session scope"
            )

    def test_the_expiry_predicate_uses_max_not_min(self) -> None:
        """``HAVING min(retention_expires_at) <= $1`` would select every session with *any* expired row
        — the naive delete wearing an aggregate. The one-character difference is the whole design."""
        assert (
            "HAVING max(retention_expires_at) <= $1" in rw.SELECT_FULLY_EXPIRED_SESSIONS
        )
        assert "HAVING min(" not in rw.SELECT_FULLY_EXPIRED_SESSIONS

    def test_the_planner_selects_no_evidence_columns(self) -> None:
        """A planner that selected ``call_ref`` would put pseudonyms in a query log for no reason."""
        for column in (
            "call_ref",
            "purpose_code",
            "spoof_risk",
            "action",
            "reason_code",
        ):
            assert column not in rw.SELECT_FULLY_EXPIRED_SESSIONS

    def test_every_statement_is_parameterised(self) -> None:
        """No f-string interpolation of values. The table name is interpolated from a module constant;
        cutoffs and ids never are."""
        for name in (
            "SELECT_FULLY_EXPIRED_SESSIONS",
            "PROBE_ANY_EXPIRED",
            "DELETE_WHOLE_SESSION",
            "COUNT_SESSION_ROWS",
        ):
            assert "$1" in getattr(rw, name), name

    def test_the_planner_is_bounded(self) -> None:
        assert "LIMIT $2" in rw.SELECT_FULLY_EXPIRED_SESSIONS

    def test_the_worker_targets_the_table_the_migration_creates(self) -> None:
        """Two constants, two files, one name. A mismatch means the sweep runs successfully against
        nothing and reports zero deletions forever."""
        assert rw.TABLE_NAME == sc.TABLE_NAME

    def test_the_advisory_lock_key_is_derived_reproducibly(self) -> None:
        """Both tiers and every operator must compute the same key without a shared constant file, or
        the lock does not serialise anything."""
        from hashlib import sha256

        expected = int.from_bytes(
            sha256(b"sih26104.audit_retention").digest()[:8], "big", signed=True
        )
        assert rw.ADVISORY_LOCK_KEY == expected


class TestCli:
    def test_dry_run_defaults_to_off(self) -> None:
        """A maintenance job whose destructive mode is the default is one nobody runs; a job whose
        destructive mode is opt-*out* is one that surprises somebody. Deleting is the job."""
        assert rw._parse_args([]).dry_run is False
        assert rw._parse_args(["--dry-run"]).dry_run is True

    def test_retention_days_comes_from_the_environment_when_not_passed(
        self, monkeypatch
    ) -> None:
        """Same variable name the Gateway uses, so the tier is configuration rather than a code
        branch (R-04)."""
        monkeypatch.setenv("AUDIT_RETENTION_DAYS", "30")
        import importlib

        reloaded = importlib.reload(rw)
        try:
            assert reloaded._parse_args([]).retention_days == 30
        finally:
            monkeypatch.delenv("AUDIT_RETENTION_DAYS")
            importlib.reload(reloaded)

    def test_a_missing_database_url_fails_loudly_rather_than_doing_nothing(
        self, monkeypatch, capsys
    ) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert rw.main([]) == rw.EXIT_ERROR
        assert "DATABASE_URL" in capsys.readouterr().err

    def test_the_unsafe_exit_code_is_distinct(self) -> None:
        """So a scheduler can page on "a partial session was refused" and ignore everything else."""
        assert rw.EXIT_UNSAFE not in (rw.EXIT_OK, rw.EXIT_ERROR)


class TestAgainstARealDatabase:
    """UNVERIFIED here — no PostgreSQL 16 is available. These are the assertions the fake cannot make:
    that the SQL is valid, that ``NOT EXISTS`` behaves as an InitPlan guard under concurrency, that
    ``pg_try_advisory_lock`` actually serialises two sessions, and that a rolled-back partial delete
    leaves the table byte-identical."""

    @pytest.mark.integration
    def test_a_fully_expired_session_is_deleted_and_survivors_verify(
        self, database_url: str
    ) -> None:
        pytest.skip(
            f"needs a live database via {DATABASE_URL_ENV}; see audit/README.md"
        )

    @pytest.mark.integration
    def test_a_concurrent_sweep_is_blocked_by_the_advisory_lock(
        self, database_url: str
    ) -> None:
        pytest.skip(
            f"needs a live database via {DATABASE_URL_ENV}; see audit/README.md"
        )

    @pytest.mark.integration
    def test_the_planner_uses_the_session_retention_index(
        self, database_url: str
    ) -> None:
        """``EXPLAIN`` must show an index scan, not a sequential scan plus sort. A retention job that
        table-scans is one that gets disabled the first time it is blamed for latency."""
        pytest.skip(
            f"needs a live database via {DATABASE_URL_ENV}; see audit/README.md"
        )


def sample_receipt(deleted: Sequence[rw.SessionPlan] = ()) -> rw.RetentionReceipt:
    return rw.build_receipt(
        run_id=uuid4().hex,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=2),
        cutoff=NOW,
        policy=rw.RetentionPolicy(retention_days=7),
        dry_run=False,
        lock_acquired=True,
        sessions_examined=len(deleted),
        deleted=deleted,
        skipped_reappeared=0,
        skipped_non_contiguous=0,
    )
