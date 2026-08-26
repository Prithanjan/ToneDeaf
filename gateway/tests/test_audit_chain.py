"""Audit hash-chain tests.

The chain is what lets a feature-only audit trail be shown to be tamper-evident. So the tests that
matter are the ones that try to tamper: edit a field, delete a row, reorder two rows, re-key the chain.
Each must be caught, and caught at the right index — a verifier that only says "something changed" is
a checksum, not evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from app.audit.chain import (
    CHAIN_FIELDS,
    EXCLUDED_FIELDS,
    ChainFieldError,
    _assert_not_forbidden,
    canonicalize,
    chain_events,
    event_hash,
    verify_chain,
)
from app.constants import GENESIS_PREV_HASH
from tests.conftest import TEST_CHAIN_KEY, audit_event

OTHER_KEY = b"different-chain-key-also-not-real-00"


def stored(events: list[dict[str, Any]], key: bytes = TEST_CHAIN_KEY) -> list[dict[str, Any]]:
    """Attach computed hashes, producing rows shaped like what the database holds."""
    rows = []
    for event, (prev, current) in zip(events, chain_events(key, events), strict=True):
        rows.append({**event, "prev_event_hash": prev, "event_hash": current})
    return rows


class TestFieldSet:
    def test_field_list_is_explicit_and_ordered(self) -> None:
        """Decision D-9: never SELECT *. With SELECT * any additive migration silently changes what
        gets hashed and every historical hash becomes unverifiable."""
        assert isinstance(CHAIN_FIELDS, tuple)
        assert len(CHAIN_FIELDS) == len(set(CHAIN_FIELDS))
        assert CHAIN_FIELDS[0] == "tenant_id"

    def test_excluded_fields_do_not_overlap_the_hashed_set(self) -> None:
        assert not set(CHAIN_FIELDS) & set(EXCLUDED_FIELDS)

    def test_retention_is_excluded_from_the_hash(self) -> None:
        """A retention-policy change must not invalidate history."""
        assert "retention_expires_at" in EXCLUDED_FIELDS
        event = audit_event(1)
        baseline = event_hash(TEST_CHAIN_KEY, event, GENESIS_PREV_HASH)
        with_retention = event_hash(
            TEST_CHAIN_KEY,
            {**event, "retention_expires_at": datetime(2030, 1, 1, tzinfo=UTC)},
            GENESIS_PREV_HASH,
        )
        assert baseline == with_retention

    def test_surrogate_key_is_excluded_from_the_hash(self) -> None:
        event = audit_event(1)
        assert event_hash(TEST_CHAIN_KEY, event, GENESIS_PREV_HASH) == event_hash(
            TEST_CHAIN_KEY, {**event, "event_id": 4242}, GENESIS_PREV_HASH
        )


class TestCanonicalization:
    def test_key_order_does_not_change_the_serialization(self) -> None:
        """The encoder sorts keys, so a driver returning columns in a different order cannot change
        the hash. CHAIN_FIELDS' order is for human review only."""
        event = audit_event(1)
        shuffled = dict(reversed(list(event.items())))
        assert canonicalize(event) == canonicalize(shuffled)

    def test_missing_canonical_field_is_refused(self) -> None:
        event = audit_event(1)
        del event["purpose_code"]
        with pytest.raises(ChainFieldError, match="missing canonical fields"):
            canonicalize(event)

    def test_unknown_field_is_refused(self) -> None:
        """Strict in BOTH directions. A silently-ignored extra field is how audio-adjacent data would
        end up stored but unhashed — present in the table, outside the tamper evidence."""
        with pytest.raises(ChainFieldError, match="unknown fields"):
            canonicalize({**audit_event(1), "operator_note": "sounds fake to me"})

    @pytest.mark.privacy
    @pytest.mark.parametrize(
        "name",
        [
            "audio_blob",
            "pcm_window",
            "waveform_ref",
            "transcript",
            "speaker_embedding",
            "phone_number",
            "msisdn",
            "caller_name",
            "AUDIO_SNIPPET",
        ],
    )
    def test_forbidden_field_names_are_refused_by_substring(self, name: str) -> None:
        """rules.md R-15. Caught by substring and case-insensitively, so audio_blob_v2 and
        AudioBlob are both refused — the deny-list is not a list of exact column names someone can
        route around with a rename."""
        with pytest.raises(ChainFieldError, match="forbidden field name"):
            canonicalize({**audit_event(1), name: b"\x00\x01"})

    @pytest.mark.privacy
    def test_deny_list_beats_the_unknown_field_check(self) -> None:
        """Ordering matters, not just the outcome.

        Every forbidden name is by construction absent from CHAIN_FIELDS, so if the unknown-field
        check ran first this deny-list could never fire. The event would still be rejected — but the
        error would read "unknown field", and a deny-list that cannot report a deny-list violation is
        one nobody can trust in a privacy review.
        """
        with pytest.raises(ChainFieldError) as caught:
            canonicalize({**audit_event(1), "raw_audio_pcm": b"\x00"})
        assert "forbidden" in str(caught.value)
        assert "unknown" not in str(caught.value)

    @pytest.mark.privacy
    def test_canonical_field_list_itself_is_deny_listed(self) -> None:
        """The dangerous change is not a stray key on one event — it is someone adding audio_blob to
        CHAIN_FIELDS and the migration in the same commit, at which point every per-event check
        passes. chain.py runs this at import, so that commit cannot even be imported."""
        _assert_not_forbidden(CHAIN_FIELDS, context="CHAIN_FIELDS")
        _assert_not_forbidden(EXCLUDED_FIELDS, context="EXCLUDED_FIELDS")
        with pytest.raises(ChainFieldError, match="forbidden field name"):
            _assert_not_forbidden((*CHAIN_FIELDS, "audio_blob"), context="CHAIN_FIELDS")

    def test_naive_timestamp_is_refused(self) -> None:
        """Guessing UTC would produce a chain that verifies on one machine and fails on another."""
        with pytest.raises(ChainFieldError, match="timezone-aware"):
            canonicalize({**audit_event(1), "occurred_at": datetime(2026, 8, 26, 12, 0, 0)})

    def test_equivalent_timestamps_in_different_zones_hash_identically(self) -> None:
        utc = datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)
        ist = utc.astimezone(timezone(timedelta(hours=5, minutes=30)))
        assert canonicalize({**audit_event(1), "occurred_at": utc}) == canonicalize(
            {**audit_event(1), "occurred_at": ist}
        )

    def test_risk_is_serialized_at_fixed_precision(self) -> None:
        """The column is numeric(5,4). A bare float would hash differently depending on its repr, so
        a value that round-trips through the database would stop verifying."""
        assert '"spoof_risk":"0.5000"' in canonicalize(audit_event(1, spoof_risk=0.5))
        assert canonicalize(audit_event(1, spoof_risk=0.5)) == canonicalize(
            audit_event(1, spoof_risk=0.50000)
        )

    def test_null_risk_is_distinct_from_zero(self) -> None:
        """Lifecycle events carry no score. None must not collapse to "0.0000", or a session.open row
        and a genuine zero-risk window would be indistinguishable in the evidence."""
        assert event_hash(
            TEST_CHAIN_KEY, audit_event(1, spoof_risk=None), GENESIS_PREV_HASH
        ) != event_hash(TEST_CHAIN_KEY, audit_event(1, spoof_risk=0.0), GENESIS_PREV_HASH)

    def test_quality_flag_order_does_not_matter(self) -> None:
        assert canonicalize(audit_event(1, quality_flags=["clipping", "low_snr"])) == canonicalize(
            audit_event(1, quality_flags=["low_snr", "clipping"])
        )

    def test_empty_and_null_flags_agree(self) -> None:
        assert canonicalize(audit_event(1, quality_flags=None)) == canonicalize(
            audit_event(1, quality_flags=[])
        )


class TestChaining:
    def test_first_event_chains_from_genesis(self) -> None:
        ((prev, _),) = chain_events(TEST_CHAIN_KEY, [audit_event(1)])
        assert prev == GENESIS_PREV_HASH
        assert len(GENESIS_PREV_HASH) == 32

    def test_each_link_carries_its_predecessor(self) -> None:
        links = chain_events(TEST_CHAIN_KEY, [audit_event(i) for i in range(1, 5)])
        for (_, current), (next_prev, _) in zip(links, links[1:], strict=False):
            assert next_prev == current

    def test_hashes_are_32_bytes(self) -> None:
        for prev, current in chain_events(TEST_CHAIN_KEY, [audit_event(i) for i in range(1, 4)]):
            assert len(prev) == len(current) == 32

    def test_identical_events_at_different_positions_hash_differently(self) -> None:
        """The prev hash is mixed in, so position is part of the identity. Otherwise two identical
        windows could be swapped without detection."""
        event = audit_event(1)
        links = chain_events(TEST_CHAIN_KEY, [event, event])
        assert links[0][1] != links[1][1]

    def test_wrong_length_prev_hash_is_refused(self) -> None:
        with pytest.raises(ChainFieldError, match="32 bytes"):
            event_hash(TEST_CHAIN_KEY, audit_event(1), b"\x00" * 31)

    def test_chain_is_keyed_not_a_bare_digest(self) -> None:
        """A plain SHA-256 chain can be recomputed wholesale by anyone able to write to the table,
        which makes it a checksum rather than tamper evidence."""
        event = audit_event(1)
        assert event_hash(TEST_CHAIN_KEY, event, GENESIS_PREV_HASH) != event_hash(
            OTHER_KEY, event, GENESIS_PREV_HASH
        )


class TestVerification:
    def test_intact_chain_verifies(self) -> None:
        rows = stored([audit_event(i) for i in range(1, 11)])
        result = verify_chain(TEST_CHAIN_KEY, rows)
        assert result.ok
        assert result.first_bad_index is None

    def test_empty_chain_verifies(self) -> None:
        assert verify_chain(TEST_CHAIN_KEY, []).ok

    def test_edited_field_is_caught_at_its_own_index(self) -> None:
        """The canonical tampering case: someone lowers a recorded risk score after the fact."""
        rows = stored([audit_event(i) for i in range(1, 11)])
        rows[4]["spoof_risk"] = 0.01
        result = verify_chain(TEST_CHAIN_KEY, rows)
        assert not result.ok
        assert result.first_bad_index == 4
        assert result.first_bad_event_seq == 5
        assert result.detail is not None

    def test_edited_action_is_caught(self) -> None:
        rows = stored([audit_event(i, action="hold") for i in range(1, 6)])
        rows[2]["action"] = "continue"
        assert not verify_chain(TEST_CHAIN_KEY, rows).ok

    def test_deleted_row_is_caught(self) -> None:
        """Deletion is the tampering a per-row signature would miss: each remaining signature is
        still valid. The chain catches it because the successor's stored prev no longer matches."""
        rows = stored([audit_event(i) for i in range(1, 11)])
        del rows[5]
        result = verify_chain(TEST_CHAIN_KEY, rows)
        assert not result.ok
        assert result.first_bad_index == 5
        assert "prev_event_hash" in (result.detail or "")

    def test_reordered_rows_are_caught(self) -> None:
        rows = stored([audit_event(i) for i in range(1, 11)])
        rows[3], rows[4] = rows[4], rows[3]
        result = verify_chain(TEST_CHAIN_KEY, rows)
        assert not result.ok
        assert result.first_bad_index == 3

    def test_appended_forged_row_is_caught(self) -> None:
        rows = stored([audit_event(i) for i in range(1, 6)])
        forged = audit_event(6, action="continue")
        rows.append(
            {**forged, "prev_event_hash": rows[-1]["event_hash"], "event_hash": b"\x11" * 32}
        )
        result = verify_chain(TEST_CHAIN_KEY, rows)
        assert not result.ok
        assert result.first_bad_index == 5

    def test_wrong_key_fails_at_the_first_row(self) -> None:
        """Which is exactly why the chain key must NEVER be rotated once any event exists: rotation
        does not invalidate old rows, it makes every one of them read as tampered (rules.md R-58)."""
        rows = stored([audit_event(i) for i in range(1, 6)])
        result = verify_chain(OTHER_KEY, rows)
        assert not result.ok
        assert result.first_bad_index == 0

    def test_missing_prev_hash_is_reported_not_assumed(self) -> None:
        rows = stored([audit_event(1)])
        del rows[0]["prev_event_hash"]
        result = verify_chain(TEST_CHAIN_KEY, rows)
        assert not result.ok
        assert "missing prev_event_hash" in (result.detail or "")

    def test_missing_event_hash_is_reported(self) -> None:
        rows = stored([audit_event(1)])
        del rows[0]["event_hash"]
        result = verify_chain(TEST_CHAIN_KEY, rows)
        assert not result.ok
        assert "missing event_hash" in (result.detail or "")

    def test_retention_edit_does_not_break_verification(self) -> None:
        """Rewriting retention_expires_at during a retention sweep must leave the chain intact."""
        rows = stored([audit_event(i) for i in range(1, 6)])
        for row in rows:
            row["retention_expires_at"] = datetime(2031, 1, 1, tzinfo=UTC)
        assert verify_chain(TEST_CHAIN_KEY, rows).ok

    def test_terminal_hash_anchor_detects_tail_truncation(self) -> None:
        """H-7 / BUG-11: An anchored terminal hash catches deletion of the tail row."""
        rows = stored([audit_event(i) for i in range(1, 6)])
        terminal_hash = rows[-1]["event_hash"]
        truncated = rows[:-1]  # drop the 5th event

        # Without anchor, prefix looks valid
        assert verify_chain(TEST_CHAIN_KEY, truncated).ok

        # With anchor, tail truncation is caught
        anchored_result = verify_chain(
            TEST_CHAIN_KEY, truncated, expected_terminal_hash=terminal_hash
        )
        assert not anchored_result.ok
        assert "terminal hash does not match" in (anchored_result.detail or "")

    def test_expected_count_anchor_detects_all_row_deletion(self) -> None:
        """H-7 / BUG-11: An expected count anchor catches deletion of all rows."""
        result = verify_chain(TEST_CHAIN_KEY, [], expected_count=5)
        assert not result.ok
        assert "event count 0 does not match expected 5" in (result.detail or "")
