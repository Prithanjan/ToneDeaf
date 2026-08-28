# ruff: noqa: E402
"""Unit and Contract Tests for Database 3-Check Privacy Verification.

Verifies that:
1. Check 1 (Schema Deny-List) strictly asserts 26 allow-listed columns and rejects forbidden
   substrings, vector types, and extra bytea columns.
2. Check 2 (Data Row & Byte Inspection) verifies 0 bytes raw audio/acoustic features and enforces
   64-hex HMAC pseudonym compliance on `call_ref`.
3. Check 3 (Cryptographic HMAC Hash Chain) detects any altered field, deleted row, or broken link.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "gateway") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "gateway"))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

from scripts.verify_database_privacy import (
    EXACT_ALLOW_LIST_26,
    check_1_schema_deny_list,
    check_2_data_row_inspection,
    check_3_cryptographic_hash_chain,
    run_self_test,
)

from app.audit.chain import chain_events

TEST_CHAIN_KEY = b"test-key-for-privacy-verification-0"


def make_valid_event(seq: int = 0) -> dict[str, Any]:
    return {
        "tenant_id": "demo-tenant",
        "session_id": "11111111-2222-4333-8444-555555555555",
        "call_ref": "c" * 64,
        "event_seq": seq,
        "occurred_at": datetime(2026, 8, 28, 12, 0, seq, tzinfo=UTC),
        "purpose_code": "payment_release",
        "context_value_band": "medium",
        "window_seq": seq,
        "spoof_risk": 0.1234,
        "risk_state": "collecting",
        "action": "continue",
        "reason_code": "INSUFFICIENT_ELIGIBLE_WINDOWS",
        "policy_version": "0.1.0",
        "policy_bundle_sha256": "a" * 64,
        "model_version": "mock-0",
        "model_sha256": "b" * 64,
        "calibration_version": "0.1.0",
        "calibration_sha256": "d" * 64,
        "quality_flags": [],
        "detector_mode": "REAL_DETECTOR",
        "execution_provider": "CPUExecutionProvider",
        "deployment_profile": "local-cpu",
    }


def make_chained_events(count: int = 4, key: bytes = TEST_CHAIN_KEY) -> list[dict[str, Any]]:
    events = [make_valid_event(i) for i in range(count)]
    for e, (prev, digest) in zip(events, chain_events(key, events), strict=True):
        e["event_seq"] = e["window_seq"]
        e["prev_event_hash"] = prev
        e["event_hash"] = digest
    return events


class TestCheck1SchemaDenyList:
    def test_default_declared_schema_passes(self) -> None:
        res = check_1_schema_deny_list()
        assert res.passed
        assert res.check_number == 1

    def test_exact_26_columns_match(self) -> None:
        res = check_1_schema_deny_list(EXACT_ALLOW_LIST_26)
        assert res.passed

    @pytest.mark.parametrize(
        "bad_column",
        [
            "audio_pcm",
            "raw_samples",
            "caller_name",
            "phone_number",
            "voice_waveform",
            "audio_embedding",
            "caller_msisdn",
            "call_transcript",
        ],
    )
    def test_rejects_forbidden_column_substrings(self, bad_column: str) -> None:
        columns = [*EXACT_ALLOW_LIST_26, bad_column]
        res = check_1_schema_deny_list(columns)
        assert not res.passed
        assert any(bad_column in d for d in res.details)

    def test_rejects_unexpected_bytea_column(self) -> None:
        col_facts = [
            {"column_name": "event_id", "data_type": "uuid", "udt_name": "uuid"},
            {"column_name": "stray_payload", "data_type": "bytea", "udt_name": "bytea"},
            *(
                {"column_name": c, "data_type": "text", "udt_name": "text"}
                for c in EXACT_ALLOW_LIST_26
                if c != "event_id"
            ),
        ]
        res = check_1_schema_deny_list(col_facts)
        assert not res.passed
        assert any("stray_payload" in d for d in res.details)

    def test_rejects_vector_and_float_array_types(self) -> None:
        col_facts = [
            {"column_name": "embedding_vec", "data_type": "vector", "udt_name": "vector"},
            *(
                {"column_name": c, "data_type": "text", "udt_name": "text"}
                for c in EXACT_ALLOW_LIST_26
            ),
        ]
        res = check_1_schema_deny_list(col_facts)
        assert not res.passed
        assert any("vector" in d for d in res.details)

    def test_rejects_missing_column(self) -> None:
        missing_subset = [c for c in EXACT_ALLOW_LIST_26 if c != "call_ref"]
        res = check_1_schema_deny_list(missing_subset)
        assert not res.passed
        assert any("call_ref" in d for d in res.details)


class TestCheck2DataRowInspection:
    def test_valid_rows_pass(self) -> None:
        events = make_chained_events(3)
        res = check_2_data_row_inspection(events)
        assert res.passed
        assert res.check_number == 2

    def test_empty_rows_pass_cleanly(self) -> None:
        res = check_2_data_row_inspection([])
        assert res.passed

    @pytest.mark.parametrize(
        "invalid_ref",
        [
            "+14155552671",
            "john.doe@example.com",
            "session-ref-not-64-hex",
            "A" * 64,  # Uppercase not permitted (must be lowercase hex)
            "g" * 64,  # Non-hex characters
            "f" * 63,  # Too short
            "f" * 65,  # Too long
        ],
    )
    def test_rejects_non_hmac_call_ref(self, invalid_ref: str) -> None:
        events = make_chained_events(2)
        events[0]["call_ref"] = invalid_ref
        res = check_2_data_row_inspection(events)
        assert not res.passed
        assert any("call_ref" in d for d in res.details)

    @pytest.mark.parametrize(
        "forbidden_action",
        ["approve", "deny", "allow", "block", "reject", "PASSED", "BLOCKED"],
    )
    def test_rejects_forbidden_action_values(self, forbidden_action: str) -> None:
        events = make_chained_events(2)
        events[1]["action"] = forbidden_action
        res = check_2_data_row_inspection(events)
        assert not res.passed
        assert any("closed vocabulary" in d for d in res.details)

    def test_rejects_spoof_risk_out_of_range(self) -> None:
        events = make_chained_events(2)
        events[0]["spoof_risk"] = 1.45
        res = check_2_data_row_inspection(events)
        assert not res.passed
        assert any("outside [0.0, 1.0]" in d for d in res.details)

    def test_rejects_unsanctioned_binary_payload(self) -> None:
        events = make_chained_events(2)
        events[0]["audio_binary"] = b"\x00\x01\x02" * 100
        res = check_2_data_row_inspection(events)
        assert not res.passed


class TestCheck3CryptographicHashChain:
    def test_valid_chain_verifies(self) -> None:
        events = make_chained_events(5)
        res = check_3_cryptographic_hash_chain(TEST_CHAIN_KEY, events)
        assert res.passed
        assert res.check_number == 3

    def test_detects_altered_action(self) -> None:
        events = make_chained_events(4)
        events[2]["action"] = "escalate"
        res = check_3_cryptographic_hash_chain(TEST_CHAIN_KEY, events)
        assert not res.passed
        assert any("event_seq=2" in d for d in res.details)

    def test_detects_altered_spoof_risk(self) -> None:
        events = make_chained_events(4)
        events[1]["spoof_risk"] = 0.9999
        res = check_3_cryptographic_hash_chain(TEST_CHAIN_KEY, events)
        assert not res.passed
        assert any("event_seq=1" in d for d in res.details)

    def test_detects_deleted_row(self) -> None:
        events = make_chained_events(5)
        # Delete row with event_seq=2
        del events[2]
        res = check_3_cryptographic_hash_chain(TEST_CHAIN_KEY, events)
        assert not res.passed
        # Next row (event_seq=3) should fail verification due to broken prev_event_hash
        assert any("event_seq=3" in d for d in res.details)

    def test_detects_reordered_rows(self) -> None:
        events = make_chained_events(4)
        # Swap event_seq of rows 1 and 2
        events[1]["event_seq"], events[2]["event_seq"] = (
            events[2]["event_seq"],
            events[1]["event_seq"],
        )
        res = check_3_cryptographic_hash_chain(TEST_CHAIN_KEY, events)
        assert not res.passed
        assert any("chain divergence" in d for d in res.details)

    def test_detects_wrong_hmac_key(self) -> None:
        events = make_chained_events(3)
        wrong_key = b"different-secret-key-00000000000"
        res = check_3_cryptographic_hash_chain(wrong_key, events)
        assert not res.passed


class TestSelfTestFunction:
    def test_run_self_test_succeeds(self) -> None:
        code = run_self_test()
        assert code == 0
