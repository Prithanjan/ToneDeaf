"""Pseudonymization tests.

The property under test is that the raw caller reference a human typed cannot be recovered from
anything the system stores or emits. So these tests check the keying, the normalization, and — most
importantly — that no error path echoes the input back (rules.md R-17).
"""

from __future__ import annotations

import pytest

from app.security.pseudonym import (
    MAX_RAW_LENGTH,
    PseudonymError,
    call_ref,
    is_valid_call_ref,
    normalize,
)
from tests.conftest import TEST_HMAC_KEY

OTHER_KEY = b"another-pseudonym-key-not-real-00000"


class TestNormalization:
    def test_whitespace_is_stripped(self) -> None:
        assert normalize("  CALL-001  ") == "CALL-001"

    def test_nfkc_collapses_full_width_digits(self) -> None:
        """NFKC over NFC deliberately: compatibility folding also collapses full-width digits, which
        is a realistic input variation for the target region. Without it, two visually identical
        references entered on different keyboards would silently split one session's audit trail."""
        assert normalize("CALL-００１") == "CALL-001"

    def test_visually_identical_inputs_yield_one_pseudonym(self) -> None:
        assert call_ref(TEST_HMAC_KEY, " CALL-００１ ") == call_ref(TEST_HMAC_KEY, "CALL-001")

    @pytest.mark.parametrize("raw", ["", "   ", "\t\n", "　"])
    def test_empty_after_normalization_is_refused(self, raw: str) -> None:
        with pytest.raises(PseudonymError):
            normalize(raw)

    def test_overlong_input_is_refused(self) -> None:
        with pytest.raises(PseudonymError):
            normalize("x" * (MAX_RAW_LENGTH + 1))

    def test_max_length_is_accepted(self) -> None:
        assert len(normalize("x" * MAX_RAW_LENGTH)) == MAX_RAW_LENGTH

    def test_non_string_is_refused(self) -> None:
        with pytest.raises(PseudonymError):
            normalize(12345)  # type: ignore[arg-type]


class TestPseudonym:
    def test_shape_is_64_hex_characters(self) -> None:
        value = call_ref(TEST_HMAC_KEY, "CALL-001")
        assert len(value) == 64
        assert is_valid_call_ref(value)

    def test_deterministic_for_the_same_key(self) -> None:
        """Must be stable, or one session's events would chain under two different call_refs and the
        audit trail would not be queryable."""
        assert call_ref(TEST_HMAC_KEY, "CALL-001") == call_ref(TEST_HMAC_KEY, "CALL-001")

    def test_distinct_inputs_give_distinct_pseudonyms(self) -> None:
        assert call_ref(TEST_HMAC_KEY, "CALL-001") != call_ref(TEST_HMAC_KEY, "CALL-002")

    @pytest.mark.privacy
    def test_keyed_not_a_bare_hash(self) -> None:
        """The reference space in a demo is small and highly guessable — CALL-001, a phone number —
        so an unkeyed digest would be reversible by dictionary attack and would not be a pseudonym
        at all. Changing the key must change the output."""
        assert call_ref(TEST_HMAC_KEY, "CALL-001") != call_ref(OTHER_KEY, "CALL-001")

    @pytest.mark.privacy
    def test_domain_separation_is_applied(self) -> None:
        """A second pseudonym type must get its own tag rather than sharing this key, so the two
        cannot be cross-correlated. Verified by showing the output is not a plain HMAC of the input."""
        import hmac
        from hashlib import sha256

        plain = hmac.new(TEST_HMAC_KEY, b"CALL-001", sha256).hexdigest()
        assert call_ref(TEST_HMAC_KEY, "CALL-001") != plain

    def test_short_key_is_refused(self) -> None:
        with pytest.raises(PseudonymError):
            call_ref(b"too-short", "CALL-001")

    @pytest.mark.privacy
    @pytest.mark.parametrize(
        "raw",
        [
            "+919812345678",
            "Ramesh Kumar",
            "x" * 200,
            "acct/1234-5678",
            "  \t  ",  # whitespace-only: refused, and the message must not echo it back
        ],
    )
    def test_no_error_message_contains_the_input(self, raw: str) -> None:
        """rules.md R-17, checked on every failure path.

        This is the leak that would otherwise be invisible: pseudonymization is correct, but the
        ValueError raised on a malformed reference carries the phone number into the log, the traceback,
        and CloudWatch. The empty string is deliberately not a case here — it is a substring of every
        message, so the assertion would be vacuous. Emptiness is covered by
        ``test_empty_after_normalization_is_refused``.
        """
        try:
            call_ref(TEST_HMAC_KEY, raw)
        except PseudonymError as exc:
            assert raw not in str(exc)
            assert raw not in repr(exc.args)


class TestShapeCheck:
    @pytest.mark.parametrize(
        "value",
        [
            "CALL-001",  # a raw reference where a pseudonym belongs
            "a" * 63,  # too short
            "a" * 65,  # too long
            "A" * 64,  # uppercase hex is not what we emit
            "g" * 64,  # not hex
            "",
        ],
    )
    def test_rejects_anything_that_is_not_our_pseudonym(self, value: str) -> None:
        """A client that sends a raw reference where a pseudonym belongs fails here, which is how a
        client-side mistake is caught before the raw value can be stored."""
        assert not is_valid_call_ref(value)

    def test_rejects_non_strings(self) -> None:
        assert not is_valid_call_ref(None)  # type: ignore[arg-type]
        assert not is_valid_call_ref(12345)  # type: ignore[arg-type]
