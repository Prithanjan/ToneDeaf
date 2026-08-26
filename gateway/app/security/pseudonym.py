"""Caller-reference pseudonymization — PURE, no I/O (rules.md R-53).

The raw ``client_call_ref`` a human typed exists in exactly one place: the body of
``POST /api/v1/sessions``, inside Gateway process memory, for the duration of that request. This
module converts it to an opaque ``call_ref`` before it can reach a response body, a WSS message, a
log line, a gRPC request, a database row, a metric label, or a webhook payload (rules.md R-16).

Every downstream component — including the Scorer and the audit table — sees only the pseudonym.
"""

from __future__ import annotations

import hmac
import unicodedata
from hashlib import sha256
from typing import Final

#: Domain separation. A single key is used for one purpose only; if a second pseudonym type is ever
#: added it gets its own tag rather than sharing this one, so the two cannot be cross-correlated.
_DOMAIN: Final[bytes] = b"sih26104/call_ref/v1\x00"

MAX_RAW_LENGTH: Final[int] = 128


class PseudonymError(ValueError):
    """The input cannot be pseudonymized.

    Messages here never include the offending value — that is the exact leak path rules.md R-17
    closes.
    """


def normalize(raw: str) -> str:
    """NFKC-normalize and strip surrounding whitespace.

    Without normalization, two visually identical references entered on different keyboards produce
    different pseudonyms and silently split one session's audit trail in two. NFKC is chosen over
    NFC because compatibility folding also collapses full-width digits, which is a realistic input
    variation for the target region.
    """
    if not isinstance(raw, str):
        raise PseudonymError("client_call_ref must be a string")
    value = unicodedata.normalize("NFKC", raw).strip()
    if not value:
        raise PseudonymError("client_call_ref must not be empty")
    if len(value) > MAX_RAW_LENGTH:
        raise PseudonymError(f"client_call_ref exceeds {MAX_RAW_LENGTH} characters")
    return value


def call_ref(hmac_key: bytes, raw: str) -> str:
    """Return the 64-character lowercase hex pseudonym for a raw caller reference.

    Keyed HMAC rather than a bare hash: the reference space in a demo is small and highly
    guessable (``CALL-001``, a phone number), so an unkeyed digest would be trivially reversible by
    dictionary attack and would not be a pseudonym at all.
    """
    if len(hmac_key) < 32:
        raise PseudonymError("pseudonymization key must be at least 32 bytes")
    return hmac.new(hmac_key, _DOMAIN + normalize(raw).encode("utf-8"), sha256).hexdigest()


def is_valid_call_ref(value: str) -> bool:
    """Shape check for a value arriving from a client (e.g. in ``session.open``).

    A caller that sends a raw reference where a pseudonym belongs fails this check, which is how a
    client-side mistake is caught before the raw value can be stored.
    """
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )
