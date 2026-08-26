"""Shared fixtures.

Two rules govern everything in this directory:

* **No test constructs a 648-byte frame or an 81,920-byte window from a literal number.** Sizes come
  from ``app.constants``. A test that hardcoded 648 would still pass if someone changed the constant
  and broke the client, which is the exact failure the constant exists to prevent (rules.md R-23).
* **No test contains a plausible-looking secret.** Keys below are obvious placeholders, long enough to
  satisfy the 32-byte minimum and nothing more.
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime
from typing import Any

import pytest

from app.constants import BYTES_PER_FRAME_PAYLOAD, SAMPLES_PER_FRAME, SEQ_STRUCT

TEST_CHAIN_KEY = b"test-chain-key-not-a-real-secret-000"
TEST_TICKET_KEY = b"test-ticket-key-not-a-real-secret-00"
TEST_HMAC_KEY = b"test-pseudonym-key-not-a-real-secret"


def make_frame(seq: int, *, fill: int = 0, payload: bytes | None = None) -> bytes:
    """Build one well-formed 648-byte wire frame.

    ``fill`` is written as every sample so a test can tell two frames apart by content without caring
    what the samples mean.
    """
    body = (
        payload
        if payload is not None
        else struct.pack(f"<{SAMPLES_PER_FRAME}h", *([fill] * SAMPLES_PER_FRAME))
    )
    return struct.pack(SEQ_STRUCT, seq) + body


def make_samples(fill: int = 0) -> list[int]:
    """One frame's worth of int16 sample values."""
    return [fill] * SAMPLES_PER_FRAME


@pytest.fixture
def frame_payload() -> bytes:
    return b"\x00" * BYTES_PER_FRAME_PAYLOAD


def audit_event(
    event_seq: int,
    *,
    spoof_risk: float | None = 0.5,
    action: str = "continue",
    risk_state: str = "collecting",
    **overrides: Any,
) -> dict[str, Any]:
    """A complete, canonical audit event.

    Every field in ``CHAIN_FIELDS`` is present because ``canonicalize`` is strict in both directions;
    a helper that omitted one would make every chain test fail for the same uninteresting reason.
    """
    event: dict[str, Any] = {
        "tenant_id": "demo-tenant",
        "session_id": "11111111-1111-4111-8111-111111111111",
        "call_ref": "a" * 64,
        "event_seq": event_seq,
        "occurred_at": datetime(2026, 8, 26, 12, 0, event_seq % 60, tzinfo=UTC),
        "purpose_code": "payment_authorization",
        "context_value_band": "medium",
        "window_seq": event_seq,
        "spoof_risk": spoof_risk,
        "risk_state": risk_state,
        "action": action,
        "reason_code": "INSUFFICIENT_ELIGIBLE_WINDOWS",
        "policy_version": "0.1.0-placeholder",
        "policy_bundle_sha256": "b" * 64,
        "model_version": "mock-0",
        "model_sha256": "c" * 64,
        "calibration_version": "0.0.0-placeholder",
        "calibration_sha256": "d" * 64,
        "quality_flags": [],
        "detector_mode": "MOCK_SMOKE",
        "execution_provider": "CPUExecutionProvider",
        "deployment_profile": "local-cpu",
    }
    event.update(overrides)
    return event
