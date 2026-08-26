"""Everything this process says about itself: the startup banner and the log sink.

The two live together because they are the same control surface. The banner exists to make the parity
set checkable in front of a human (architecture.md §5.1: *"Both tiers must print the entire parity set
in the startup banner"*), and the log sink exists to make sure nothing else escapes while it does. If
the banner were emitted through an unredacted path it would BE the leak path — it prints artifact
identity, and artifact identity is one field away from session identity.

REDACTION IS AT THE SINK, NOT AT THE CALL SITE.
Same reasoning as ``gateway/app/telemetry/logging.py``, which this mirrors: a control applied where the
record is serialized still catches a ``logger.info("window=%s", pcm)`` written by a future feature,
whereas a control applied at each call site only catches the call sites that exist today. This process
handles 81,920-byte audio payloads on every request, and "log the buffer to debug the parser" is the
single most likely way audio escapes (rules.md R-14). So ``bytes`` is never rendered — it is replaced
with its length — and only allow-listed structured keys are emitted at all.

The allow-list here is a SUBSET of the Gateway's. The Scorer has no session_id, no purpose_code, no
risk_state, and no action: it cannot log what it is not given, and enumerating keys it will never hold
would invite someone to start holding them.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any, Final

from app.calibration import Calibration
from app.config import ArtifactState, DetectorMode, ScorerSettings
from app.contract import CONTRACT_ID, WINDOW_BYTES, WINDOW_SAMPLES

SERVICE_NAME: Final[str] = "scorer"

#: Every key is an identifier, a version, a count, or an enum value. There is no key here that could
#: carry free text supplied by a caller, and none that could carry a sample.
ALLOWED_EXTRA_KEYS: Final[frozenset[str]] = frozenset(
    {
        "call_ref",  # the HMAC pseudonym the Gateway sends as session_ref, never a raw reference
        "window_seq",
        "spoof_risk",
        "raw_score",
        "eligible",
        "quality_flags",
        "model_version",
        "calibration_version",
        "detector_mode",
        "execution_provider",
        "deployment_profile",
        "artifact_state",
        "code",
        "duration_ms",
        "scorer_latency_us",
        "bytes_expected",
        "bytes_actual",
        "git_commit",
        "component",
    }
)

_DIGIT_RUN: Final[re.Pattern[str]] = re.compile(r"(?<!\w)(?:\+?\d[\d\-\s()]{8,}\d)(?!\w)")
_MAX_MESSAGE_CHARS: Final[int] = 2_000

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

_BANNER_WIDTH: Final[int] = 78

#: The two-line block that makes mock mode impossible to skim past (rules.md R-46).
MOCK_BANNER_MARKER: Final[str] = "** MOCK SMOKE MODE — THIS IS NOT A DETECTOR **"


def _scrub(value: Any) -> Any:
    if isinstance(value, bytes | bytearray | memoryview):
        # Never render audio. The length is the only useful debugging fact and the only safe one.
        return f"[{len(value)} bytes withheld]"
    if isinstance(value, str):
        return _DIGIT_RUN.sub("[redacted-digits]", value[:_MAX_MESSAGE_CHARS])
    if isinstance(value, int | float | bool) or value is None:
        return value
    if isinstance(value, list | tuple):
        return [_scrub(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _scrub(item) for key, item in value.items()}
    return _DIGIT_RUN.sub("[redacted-digits]", str(value)[:_MAX_MESSAGE_CHARS])


def _iso_utc(epoch_seconds: float) -> str:
    """RFC 3339 UTC with milliseconds — same format as the Gateway, so the two logs interleave."""
    stamp = datetime.fromtimestamp(epoch_seconds, tz=UTC)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class RedactingJsonFormatter(logging.Formatter):
    """One JSON object per record, with ``bytes`` never rendered and unknown keys dropped."""

    def __init__(self, *, deployment_profile: str, git_commit: str, detector_mode: str):
        super().__init__()
        self._profile = deployment_profile
        self._commit = git_commit
        self._detector_mode = detector_mode

    def format(self, record: logging.LogRecord) -> str:
        try:
            message = record.getMessage()
        except Exception:
            # Deliberately broad, and deliberately not re-raised: a malformed format string in a log
            # call must not take down the scoring path. Losing one line's text is a nuisance; losing
            # the process because a "%s" had no argument is an outage caused by telemetry.
            message = record.msg if isinstance(record.msg, str) else "<unformattable log record>"

        payload: dict[str, Any] = {
            "ts": _iso_utc(record.created),
            "level": record.levelname,
            "logger": record.name,
            "service": SERVICE_NAME,
            "deployment_profile": self._profile,
            "git_commit": self._commit,
            # On EVERY line, not only the ones that mention a score. A grep for a latency number in a
            # log file must not be able to find a mock measurement without the label attached
            # (rules.md R-46).
            "detector_mode": self._detector_mode,
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
            # Names only, never values. Visible enough that a developer notices their field vanished,
            # and safe even when the value was the thing that mattered.
            payload["dropped_keys"] = sorted(dropped)

        if record.exc_info:
            payload["exception"] = _scrub(self.formatException(record.exc_info))

        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(settings: ScorerSettings) -> None:
    """Install the redacting formatter as the ONLY handler on the root logger.

    Replaces existing handlers rather than adding to them. A second handler carrying a default
    formatter would silently defeat every control in this module — and grpcio installs its own logging
    on first import — so ``grpc`` is re-pointed at the root here as well.
    """
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        RedactingJsonFormatter(
            deployment_profile=settings.deployment_profile.value,
            git_commit=settings.git_commit,
            detector_mode=settings.detector_mode.value,
        )
    )

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    for name in ("grpc", "grpc._channel", "asyncio", "onnxruntime"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def build_banner(
    settings: ScorerSettings,
    *,
    calibration: Calibration,
    model_version: str,
    model_sha256: str,
    execution_provider: str,
    artifact_state: ArtifactState,
    contract_vector_parity_ok: bool,
    proto_sha256: str,
) -> list[str]:
    """The parity set, as lines. Pure — no I/O, no clock — so its content is unit-testable.

    A banner that is only ever seen by eye is a banner whose warning blocks can silently stop being
    emitted. Building it as a value means ``tests/test_banner.py`` can assert that mock mode and a
    placeholder calibration each produce their warning block, which is the property rules.md R-46 and
    R-11 actually need.
    """
    rule = "=" * _BANNER_WIDTH
    divider = "-" * _BANNER_WIDTH

    lines = [
        rule,
        "SIH26104 Voice Integrity Control Plane — Scorer",
        rule,
        f"  deployment_profile     {settings.deployment_profile.value}",
        f"  execution_provider     {execution_provider}  "
        f"(configured: {settings.execution_provider.value})",
        f"  git_commit             {settings.git_commit}",
        f"  proto_contract         sha256={proto_sha256[:16]}…",
        f"  window_contract        {CONTRACT_ID}  "
        f"({WINDOW_SAMPLES} samples / {WINDOW_BYTES} bytes)",
        f"  model_version          {model_version}  sha256={model_sha256[:16]}…",
        f"  calibration_version    {calibration.version}  sha256={calibration.sha256[:16]}…",
        f"  calibration_method     {calibration.method}  "
        f"(slope={calibration.slope:+.6g} intercept={calibration.intercept:+.6g})",
        f"  calibration_fitted_on  {calibration.fitted_on}",
        f"  detector_mode          {settings.detector_mode.value}",
        f"  contract_vector_parity {contract_vector_parity_ok}",
        f"  artifact_state         {artifact_state.value}"
        + (
            ""
            if artifact_state is settings.artifact_state
            else f"  (declared: {settings.artifact_state.value}, CAPPED)"
        ),
        f"  grpc                   0.0.0.0:{settings.grpc_port}  "
        f"workers={settings.grpc_max_workers} max_rpcs={settings.grpc_max_concurrent_rpcs}",
        divider,
    ]

    if settings.detector_mode is not DetectorMode.REAL:
        # rules.md R-46. Worded so that a screenshot of this block cannot be presented as evidence of
        # detection, and so that a reader who knows nothing about the project understands it.
        lines += [
            f"  {MOCK_BANNER_MARKER}",
            "  Scores are a deterministic function of the input bytes. They measure the transport",
            "  path only. No model is loaded. Nothing printed by this process today is evidence of",
            "  spoof detection, and no latency figure from it describes a detector.",
            divider,
        ]

    if not calibration.is_policy_eligible:
        # rules.md R-11. No probability language until a calibration artifact says so.
        lines += [
            f"  ** CALIBRATION STATUS {calibration.status.upper()} **",
            "  spoof_risk must not be described as a probability in UI, logs, or docs.",
            divider,
        ]

    if artifact_state is not ArtifactState.POLICY_ELIGIBLE:
        lines += [
            f"  ** ARTIFACT STATE {artifact_state.value.upper()} — NOT POLICY ELIGIBLE **",
            divider,
        ]

    if not contract_vector_parity_ok:
        # frame_contract.md §6: the fixed vector is re-scored at every startup precisely so a
        # mismatched artifact pairing cannot reach a demo unnoticed. "Unverified" is reported as such
        # rather than as passing.
        lines += [
            "  ** CONTRACT VECTOR PARITY NOT VERIFIED **",
            "  The fixed 40,960-sample vector was not scored against a declared expected value.",
            divider,
        ]

    lines.append(rule)
    return lines


def emit_banner(lines: list[str], logger: logging.Logger) -> None:
    """Send the banner through the redacting sink, one record per line.

    Through the logger rather than ``print`` so the banner is captured by whatever collects the rest of
    the process's output — CloudWatch on ECS, ``docker compose logs`` locally. A banner on stdout while
    the logs went elsewhere is a banner nobody sees on the tier where it matters most.
    """
    for line in lines:
        logger.info(line, extra={"component": "banner"})
