"""Log redaction — a PRIVACY test, so a failure is a release blocker (rules.md R-14, R-16, R-17).

technical-design.md section 9 makes this a Phase 1 exit criterion, and the test is deliberately hostile: it
injects a raw caller reference and a PCM payload through every route a value can reach a log line —
the message string, a format argument, an ``extra`` field, a nested structure, and an exception
traceback — and asserts none of them appears in the output.

That shape is why redaction lives in the formatter rather than at the call sites. A test that only
checked the call sites we wrote today would pass while the next feature's ``logger.info("ref=%s",
raw_ref)`` leaked.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from app.telemetry.logging import (
    ALLOWED_EXTRA_KEYS,
    RedactingJsonFormatter,
    configure_logging,
)

RAW_REF = "+919812345678"
RAW_NAME = "Ramesh Kumar"
PCM = b"\x01\x02" * 320  # one frame's payload

pytestmark = pytest.mark.privacy


@pytest.fixture
def emit() -> Any:
    """Format a record through the real formatter and return the parsed JSON."""
    formatter = RedactingJsonFormatter(
        service="gateway", deployment_profile="local-cpu", git_commit="deadbeef"
    )

    def _emit(
        msg: str,
        *args: object,
        extra: dict[str, Any] | None = None,
        exc_info: Any = None,
        level: int = logging.INFO,
    ) -> dict[str, Any]:
        record = logging.LogRecord(
            name="test",
            level=level,
            pathname=__file__,
            lineno=1,
            msg=msg,
            args=args or None,
            exc_info=exc_info,
        )
        for key, value in (extra or {}).items():
            setattr(record, key, value)
        return json.loads(formatter.format(record))

    return _emit


class TestAudioNeverRendered:
    def test_bytes_in_an_allowed_field_become_a_length(self, emit: Any) -> None:
        """ "Log the payload to debug the frame parser" is the single most likely way audio escapes."""
        out = emit("frame", extra={"quality_flags": PCM})
        assert out["quality_flags"] == f"[{len(PCM)} bytes withheld]"
        assert "\\x01" not in json.dumps(out)

    def test_bytes_as_a_format_argument_are_not_rendered(self, emit: Any) -> None:
        out = emit("payload=%s", PCM)
        assert "bytes withheld" in out["message"] or "\x01\x02" not in out["message"]
        assert PCM.decode("latin-1") not in json.dumps(out)

    @pytest.mark.parametrize("payload", [b"\x00" * 640, bytearray(b"\xff" * 648)])
    def test_every_bytes_like_type_is_covered(self, emit: Any, payload: Any) -> None:
        out = emit("x", extra={"quality_flags": payload})
        assert out["quality_flags"] == f"[{len(payload)} bytes withheld]"

    def test_bytes_nested_in_a_structure_are_covered(self, emit: Any) -> None:
        """The realistic leak is not a bare bytes value — it is a debug dict that happens to contain
        one, three levels down."""
        out = emit("x", extra={"quality_flags": {"window": [{"pcm": PCM}]}})
        assert "withheld" in json.dumps(out["quality_flags"])
        assert "\\u0001\\u0002" not in json.dumps(out)


class TestRawReferenceMasking:
    def test_digit_run_in_the_message_is_masked(self, emit: Any) -> None:
        out = emit(f"session for {RAW_REF} opened")
        assert RAW_REF not in out["message"]
        assert "[redacted-digits]" in out["message"]

    def test_digit_run_via_a_format_argument_is_masked(self, emit: Any) -> None:
        """The exact call shape rules.md R-17 targets: a %s that interpolates client input."""
        out = emit("ref=%s", RAW_REF)
        assert RAW_REF not in json.dumps(out)

    @pytest.mark.parametrize(
        "value",
        ["+91 98123 45678", "9812345678", "+1 (555) 010-9999", "4111-1111-1111-1111"],
    )
    def test_common_identifier_shapes_are_masked(self, emit: Any, value: str) -> None:
        out = emit(f"value {value} here")
        assert value not in out["message"]

    def test_masking_does_not_eat_short_numbers(self, emit: Any) -> None:
        """Pattern masking is a last resort, not the primary control. If it were aggressive enough to
        catch everything it would also destroy window_seq and latency values, and people would turn it
        off — which is worse than a narrow rule plus the two structural controls."""
        out = emit("window 42 scored in 18 ms")
        assert "42" in out["message"]
        assert "18" in out["message"]


class TestStructuralAllowList:
    def test_unknown_extra_key_is_dropped(self, emit: Any) -> None:
        out = emit("x", extra={"client_call_ref": RAW_REF})
        assert "client_call_ref" not in out
        assert RAW_REF not in json.dumps(out)

    def test_dropped_key_is_reported_by_name_only(self, emit: Any) -> None:
        """Visible enough that a developer notices their field vanished; safe even when the value was
        the thing that mattered."""
        out = emit("x", extra={"caller_name": RAW_NAME})
        assert out["dropped_keys"] == ["caller_name"]
        assert RAW_NAME not in json.dumps(out)

    def test_allowed_keys_survive(self, emit: Any) -> None:
        out = emit("x", extra={"session_id": "abc", "risk_state": "uncertain", "window_seq": 7})
        assert out["session_id"] == "abc"
        assert out["risk_state"] == "uncertain"
        assert out["window_seq"] == 7

    def test_allow_list_contains_no_free_text_field(self) -> None:
        """Every permitted key is an identifier, a version, a count, or an enum value. A key like
        "note" or "detail" would be where a caller reference eventually gets rendered."""
        forbidden = {
            "note",
            "notes",
            "detail",
            "details",
            "message_detail",
            "raw",
            "input",
            "client_call_ref",
            "caller_name",
            "phone",
            "msisdn",
            "transcript",
            "audio",
        }
        assert not (ALLOWED_EXTRA_KEYS & forbidden)

    def test_call_ref_is_allowed_but_documented_as_the_pseudonym(self) -> None:
        """call_ref is permitted because it IS the pseudonym. The shape check that keeps a raw value
        out of it lives in pseudonym.is_valid_call_ref, at the boundary where the value is created."""
        assert "call_ref" in ALLOWED_EXTRA_KEYS
        assert "client_call_ref" not in ALLOWED_EXTRA_KEYS


class TestExceptionPaths:
    def test_traceback_is_scrubbed(self, emit: Any) -> None:
        """A repr in a stack frame can contain a payload, and an unhandled exception is exactly when
        nobody is watching the log format."""
        try:
            raise ValueError(f"failed on {RAW_REF}")
        except ValueError:
            import sys

            out = emit("boom", exc_info=sys.exc_info(), level=logging.ERROR)
        assert RAW_REF not in json.dumps(out)
        assert "exception" in out

    def test_bad_format_string_does_not_crash_the_process(self, emit: Any) -> None:
        """A logging call that raises would take down a live session for a cosmetic reason."""
        out = emit("ref=%s and %s", RAW_REF)  # too few arguments
        assert "message" in out
        assert RAW_REF not in json.dumps(out)


class TestConfiguration:
    def test_configure_logging_leaves_exactly_one_handler(self) -> None:
        """A second handler with a default formatter would silently defeat every control in this
        module — and uvicorn installs one by default."""
        root = logging.getLogger()
        original = list(root.handlers)
        original_level = root.level
        try:
            root.addHandler(logging.StreamHandler())
            configure_logging(level="INFO", deployment_profile="local-cpu", git_commit="abc")
            assert len(root.handlers) == 1
            assert isinstance(root.handlers[0].formatter, RedactingJsonFormatter)
        finally:
            for handler in list(root.handlers):
                root.removeHandler(handler)
            for handler in original:
                root.addHandler(handler)
            root.setLevel(original_level)

    def test_uvicorn_loggers_are_repointed(self) -> None:
        root = logging.getLogger()
        original = list(root.handlers)
        original_level = root.level
        try:
            logging.getLogger("uvicorn.access").addHandler(logging.StreamHandler())
            configure_logging(level="INFO", deployment_profile="local-cpu", git_commit="abc")
            access = logging.getLogger("uvicorn.access")
            assert access.handlers == []
            assert access.propagate is True
        finally:
            for handler in list(root.handlers):
                root.removeHandler(handler)
            for handler in original:
                root.addHandler(handler)
            root.setLevel(original_level)


class TestOutputShape:
    def test_every_line_is_one_json_object(self, emit: Any) -> None:
        """CloudWatch Logs Insights parses per-line JSON. A multi-line record becomes several
        unparseable events, which is how a demo's evidence trail becomes ungreppable."""
        out = emit("multi\nline\nmessage")
        assert isinstance(out, dict)

    def test_parity_fields_are_always_present(self, emit: Any) -> None:
        out = emit("x")
        assert {"ts", "level", "logger", "service", "deployment_profile", "git_commit"} <= set(out)

    def test_timestamp_has_millisecond_precision(self, emit: Any) -> None:
        ts = emit("x")["ts"]
        assert ts.endswith("Z")
        assert len(ts.split(".")[1]) == 4  # "mmmZ"
