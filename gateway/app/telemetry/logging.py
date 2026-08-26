"""Structured logging with redaction enforced at the sink.

The Phase-1 log-redaction test injects a raw caller reference and a PCM payload into *every* logger
call site and asserts neither appears in the output. That test can only pass if redaction happens
where the record is serialized rather than where it is written, so this module puts it in the
formatter: a `logger.info("ref=%s", raw_ref)` somewhere in a future feature is still caught.

Three controls:

* **Structural** — only allow-listed extra keys are emitted. An unknown key is dropped and counted,
  so adding a field to a log line is as deliberate as adding a column to the audit table.
* **Type** — a ``bytes`` value is never rendered. It is replaced with its length. Audio is the one
  thing in this process that arrives as bytes in volume, and "log the payload to debug the frame
  parser" is the single most likely way it escapes (rules.md R-14).
* **Pattern** — anything shaped like a phone number or a long digit run in a message is masked, as a
  last resort for values that reach the message string by a path nobody anticipated.

None of the three is sufficient alone; the point is that a leak has to defeat all three.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any, Final

#: Keys permitted in the structured payload. Every one is an identifier, a version, a count, or an
#: enum value. There is no key here that could carry free text a caller supplied.
ALLOWED_EXTRA_KEYS: Final[frozenset[str]] = frozenset(
    {
        "session_id",
        "call_ref",  # the HMAC pseudonym only — pseudonym.is_valid_call_ref shape
        "tenant_id",
        "event_seq",
        "window_seq",
        "risk_state",
        "action",
        "reason_code",
        "spoof_risk",
        "eligible",
        "quality_flags",
        "policy_version",
        "model_version",
        "calibration_version",
        "detector_mode",
        "execution_provider",
        "deployment_profile",
        "code",
        "close_code",
        "duration_ms",
        "scorer_latency_us",
        "frames_received",
        "windows_scored",
        "discarded_frames",
        "bytes_expected",
        "bytes_actual",
        "seq_expected",
        "seq_actual",
        "request_id",
        "git_commit",
        "artifact_state",
        "component",
    }
)

_DIGIT_RUN: Final[re.Pattern[str]] = re.compile(r"(?<!\w)(?:\+?\d[\d\-\s()]{8,}\d)(?!\w)")
_MAX_MESSAGE_CHARS: Final[int] = 2_000


def _mask_digits(text: str) -> str:
    """Mask long digit runs — phone numbers, MSISDNs, account numbers."""
    return _DIGIT_RUN.sub("[redacted-digits]", text)


def _scrub(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        # Never render audio. The length is the only useful debugging fact and the only safe one.
        return f"[{len(value)} bytes withheld]"
    if isinstance(value, str):
        return _mask_digits(value[:_MAX_MESSAGE_CHARS])
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _scrub(v) for k, v in value.items()}
    return _mask_digits(str(value)[:_MAX_MESSAGE_CHARS])


def _iso_utc(epoch_seconds: float) -> str:
    """RFC 3339 UTC with milliseconds.

    Built explicitly rather than via ``Formatter.formatTime``, whose ``datefmt`` goes through
    ``time.strftime`` and has no millisecond directive — a mixed-precision timestamp makes log lines
    hard to correlate with the microsecond-precision ``occurred_at`` in the audit table.
    """
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class RedactingJsonFormatter(logging.Formatter):
    """Emits one JSON object per record, with the three controls above applied."""

    def __init__(self, *, service: str, deployment_profile: str, git_commit: str):
        super().__init__()
        self._service = service
        self._profile = deployment_profile
        self._commit = git_commit

    def format(self, record: logging.LogRecord) -> str:
        try:
            message = record.getMessage()
        except Exception:  # a bad format string must not take down the process
            message = record.msg if isinstance(record.msg, str) else "<unformattable log record>"

        payload: dict[str, Any] = {
            "ts": _iso_utc(record.created),
            "level": record.levelname,
            "logger": record.name,
            "service": self._service,
            "deployment_profile": self._profile,
            "git_commit": self._commit,
            "message": _scrub(message),
        }

        dropped: list[str] = []
        for key, value in getattr(record, "__dict__", {}).items():
            if key in _RESERVED or key.startswith("_"):
                continue
            if key in ALLOWED_EXTRA_KEYS:
                payload[key] = _scrub(value)
            else:
                dropped.append(key)

        if dropped:
            # Report the key NAMES only, never the values. Visible enough that a developer notices
            # their field vanished, and safe even when the value was the thing that mattered.
            payload["dropped_keys"] = sorted(dropped)

        if record.exc_info:
            # Formatted traceback, then scrubbed: a repr in a stack frame can contain a payload.
            payload["exception"] = _scrub(self.formatException(record.exc_info))

        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


_RESERVED: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "stacklevel",
        "thread",
        "threadName",
        "taskName",
    }
)


def configure_logging(
    *, level: str, deployment_profile: str, git_commit: str, service: str = "gateway"
) -> None:
    """Install the redacting formatter as the ONLY handler on the root logger.

    Replaces existing handlers rather than adding to them. A second handler with a default formatter
    would silently defeat every control in this module, and uvicorn installs one by default — so this
    also re-points uvicorn's own loggers at the root.
    """
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        RedactingJsonFormatter(
            service=service, deployment_profile=deployment_profile, git_commit=git_commit
        )
    )

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "asyncio", "grpc"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
