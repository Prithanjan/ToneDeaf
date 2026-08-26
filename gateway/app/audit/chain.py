"""Audit hash chain — PURE, no I/O (rules.md R-53).

Canonical serialization plus a keyed HMAC chain, so a stored audit trail can be shown to be
tamper-evident without ever having stored audio. This is the mechanism behind the "persistent
evidence" half of the product claim: the evidence is a chained sequence of *features and
decisions*, never a recording.

Two decisions here are load-bearing and easy to break by accident:

* **The field list is explicit and ordered** (decision D-9), never ``SELECT *``. With ``SELECT *``
  any future additive migration silently changes what gets hashed and every historical hash becomes
  unverifiable. An explicit list makes that a deliberate, version-bumped act.
* **``event_id`` and ``retention_expires_at`` are excluded.** A retention-policy change must not
  invalidate history, and a surrogate key carries no auditable meaning.

Changing ``CHAIN_FIELD_SET_VERSION`` is a breaking change requiring a documented re-anchor
(rules.md R-27).
"""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from typing import Any, Final, Mapping, Sequence

from app.constants import CHAIN_FIELD_SET_VERSION, GENESIS_PREV_HASH

__all__ = [
    "CHAIN_FIELDS",
    "CHAIN_FIELD_SET_VERSION",
    "ChainFieldError",
    "VerificationResult",
    "canonicalize",
    "chain_events",
    "event_hash",
    "verify_chain",
]

#: The canonical field set, in the order documented in technical-design.md section 5.3. The order is fixed
#: for human review; the JSON encoder sorts keys, so serialization does not depend on it.
CHAIN_FIELDS: Final[tuple[str, ...]] = (
    "tenant_id",
    "session_id",
    "call_ref",
    "event_seq",
    "occurred_at",
    "purpose_code",
    "context_value_band",
    "window_seq",
    "spoof_risk",
    "risk_state",
    "action",
    "reason_code",
    "policy_version",
    "policy_bundle_sha256",
    "model_version",
    "model_sha256",
    "calibration_version",
    "calibration_sha256",
    "quality_flags",
    "detector_mode",
    "execution_provider",
    "deployment_profile",
)

#: Present on a stored row but NOT hashed. Named explicitly so a reviewer can see that each
#: omission is a decision rather than an oversight.
EXCLUDED_FIELDS: Final[tuple[str, ...]] = (
    "event_id",
    "retention_expires_at",
    "prev_event_hash",
    "event_hash",
)

_FORBIDDEN_SUBSTRINGS: Final[tuple[str, ...]] = (
    "audio",
    "pcm",
    "waveform",
    "transcript",
    "embedding",
    "phone",
    "msisdn",
    "caller_name",
)


class ChainFieldError(ValueError):
    """The event does not match the canonical field set."""


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Outcome of a chain verification pass.

    Carries the FIRST divergence rather than a count, because localizing tampering is the property
    under test — a chain that only reports "something changed" is a checksum.
    """

    ok: bool
    first_bad_index: int | None = None
    first_bad_event_seq: int | None = None
    detail: str | None = None


def _format_timestamp(value: datetime | str) -> str:
    """RFC 3339, microsecond precision, UTC, ``Z`` suffix.

    Timestamp formatting is part of the hash input, so it cannot be left to whatever the database
    driver happens to return. A naive datetime is rejected rather than assumed to be UTC: guessing
    would produce a chain that verifies on one machine and fails on another.
    """
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        raise ChainFieldError("occurred_at must be timezone-aware")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _format_risk(value: float | Decimal | str | None) -> str | None:
    """Fixed 4-decimal string, matching ``numeric(5,4)`` in the schema.

    A bare float would hash differently depending on its repr; the column is exact, so the hash
    input is too. ``None`` (lifecycle events carry no score) stays ``None``, which is a distinct
    hash input from ``"0.0000"``.
    """
    if value is None:
        return None
    return f"{Decimal(str(value)):.4f}"


def _assert_not_forbidden(names: "Sequence[str] | Mapping[str, Any]", *, context: str) -> None:
    """Reject any name containing an audio- or identity-adjacent substring (rules.md R-15).

    Runs BEFORE the unknown-field check, and is applied to :data:`CHAIN_FIELDS` itself at import as
    well as to each event's keys. Both parts matter:

    * Ordering: every forbidden name is by construction absent from ``CHAIN_FIELDS``, so if the
      unknown-field check ran first this one could never fire. The event would still be rejected, but
      the error would say "unknown field" — and a deny-list that cannot report a deny-list violation
      is a deny-list nobody can trust.
    * Scope: checking the canonical field list too is what makes this a real control rather than a
      formality. The dangerous change is not a stray key on one event; it is someone adding
      ``audio_blob`` to ``CHAIN_FIELDS`` and the migration together, at which point every per-event
      check would happily pass. That import fails instead.
    """
    for name in names:
        lowered = name.lower()
        hit = next((bad for bad in _FORBIDDEN_SUBSTRINGS if bad in lowered), None)
        if hit is not None:
            raise ChainFieldError(f"forbidden field name in {context}: {name!r}")


def canonicalize(event: Mapping[str, Any]) -> str:
    """Serialize an audit event to its canonical JSON form.

    Raises:
        ChainFieldError: if a field name is forbidden, if a canonical field is missing, or if an
            unknown field is present. Strictness in both directions is the point — a silently-ignored
            extra field is how audio-adjacent data would end up stored but unhashed.
    """
    _assert_not_forbidden(event, context="event")

    missing = sorted(f for f in CHAIN_FIELDS if f not in event)
    if missing:
        raise ChainFieldError(f"missing canonical fields: {missing}")

    known = set(CHAIN_FIELDS) | set(EXCLUDED_FIELDS)
    extra = sorted(k for k in event if k not in known)
    if extra:
        raise ChainFieldError(f"unknown fields not in the canonical set: {extra}")

    payload: dict[str, Any] = {field: event[field] for field in CHAIN_FIELDS}
    payload["occurred_at"] = _format_timestamp(event["occurred_at"])
    payload["spoof_risk"] = _format_risk(event["spoof_risk"])
    payload["quality_flags"] = sorted(str(f) for f in (event["quality_flags"] or ()))
    payload["session_id"] = str(event["session_id"])
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def event_hash(chain_key: bytes, event: Mapping[str, Any], prev_hash: bytes) -> bytes:
    """Compute one link: ``HMAC_SHA256(chain_key, canonical_utf8 || prev_event_hash)``.

    Keyed, not a bare digest: a plain SHA-256 chain can be recomputed wholesale by anyone able to
    write to the table, which makes it a checksum rather than tamper *evidence*. The key lives in
    Secrets Manager and must never be rotated once any audit event exists — rotation invalidates
    every historical link (aws-setup-instructions.md section 6).
    """
    if len(prev_hash) != 32:
        raise ChainFieldError(f"prev_event_hash must be 32 bytes, got {len(prev_hash)}")
    canonical = canonicalize(event).encode("utf-8")
    return hmac.new(chain_key, canonical + prev_hash, sha256).digest()


def chain_events(
    chain_key: bytes,
    events: Sequence[Mapping[str, Any]],
    *,
    genesis: bytes = GENESIS_PREV_HASH,
) -> list[tuple[bytes, bytes]]:
    """Compute ``(prev_event_hash, event_hash)`` for an ordered event sequence."""
    out: list[tuple[bytes, bytes]] = []
    prev = genesis
    for event in events:
        current = event_hash(chain_key, event, prev)
        out.append((prev, current))
        prev = current
    return out


def verify_chain(
    chain_key: bytes,
    events: Sequence[Mapping[str, Any]],
    *,
    genesis: bytes = GENESIS_PREV_HASH,
    expected_terminal_hash: bytes | None = None,
    expected_count: int | None = None,
) -> VerificationResult:
    """Recompute forward from genesis and report the first divergent ``event_seq``.

    Each event must carry its stored ``prev_event_hash`` and ``event_hash``. Both are checked: a
    stored ``prev`` that disagrees with the recomputed chain catches a deleted or reordered row,
    while a mismatched ``event_hash`` catches an edited one.

    When ``expected_terminal_hash`` or ``expected_count`` is provided, tail truncation and total
    deletion are detected (decision H-7 / BUG-11).
    """
    if expected_count is not None and len(events) != expected_count:
        return VerificationResult(
            False,
            len(events),
            None,
            f"event count {len(events)} does not match expected {expected_count}",
        )

    prev = genesis
    for index, event in enumerate(events):
        stored_prev = event.get("prev_event_hash")
        if stored_prev is None:
            return VerificationResult(False, index, _seq(event), "row is missing prev_event_hash")
        if not hmac.compare_digest(bytes(stored_prev), prev):
            return VerificationResult(
                False, index, _seq(event), "prev_event_hash does not match the recomputed chain"
            )

        stored_hash = event.get("event_hash")
        if stored_hash is None:
            return VerificationResult(False, index, _seq(event), "row is missing event_hash")

        expected = event_hash(chain_key, event, prev)
        if not hmac.compare_digest(bytes(stored_hash), expected):
            return VerificationResult(
                False, index, _seq(event), "event_hash does not match the recomputed value"
            )
        prev = expected

    if expected_terminal_hash is not None and not hmac.compare_digest(prev, expected_terminal_hash):
        return VerificationResult(
            False,
            len(events),
            None,
            "terminal hash does not match expected anchor",
        )

    return VerificationResult(True)


def _seq(event: Mapping[str, Any]) -> int | None:
    value = event.get("event_seq")
    return int(value) if value is not None else None


# Import-time guard. The failure mode this catches is a reviewer approving an additive migration that
# adds an audio-adjacent column and the matching CHAIN_FIELDS entry in the same commit — at which
# point every per-event check passes and the deny-list test in audit/tests would be the only thing
# left. This makes the module refuse to import instead.
_assert_not_forbidden(CHAIN_FIELDS, context="CHAIN_FIELDS")
_assert_not_forbidden(EXCLUDED_FIELDS, context="EXCLUDED_FIELDS")
