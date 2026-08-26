"""The banner and the log sink: what this process says about itself, and what it refuses to say.

The banner is the only place a human sees the whole parity set at once (architecture.md §5.1), and the log
sink is the reason printing it is safe. Both are tested because both fail silently: a warning block that
stops being emitted still produces a banner, and a redaction that stops applying still produces logs.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.banner import (
    ALLOWED_EXTRA_KEYS,
    MOCK_BANNER_MARKER,
    SERVICE_NAME,
    RedactingJsonFormatter,
    build_banner,
    configure_logging,
    emit_banner,
)
from app.calibration import Calibration, placeholder_calibration
from app.config import ArtifactState, ScorerSettings, load_settings


def _banner(settings: ScorerSettings, calibration: Calibration, **overrides: object) -> list[str]:
    kwargs: dict[str, object] = {
        "model_version": "mock-smoke-not-a-detector",
        "model_sha256": "0" * 64,
        "execution_provider": "CPUExecutionProvider",
        "artifact_state": ArtifactState.RESEARCH_ONLY,
        "contract_vector_parity_ok": False,
        "proto_sha256": "a" * 64,
    }
    kwargs.update(overrides)
    return build_banner(settings, calibration=calibration, **kwargs)  # type: ignore[arg-type]


class TestBannerWarningBlocks:
    """A banner nobody asserts on is a banner whose warnings can silently stop appearing."""

    def test_mock_mode_emits_the_unmissable_block(self, mock_settings: ScorerSettings) -> None:
        """rules.md R-46. Prevents a screenshot of this banner being presented as evidence of detection.

        The block is worded for a reader who knows nothing about the project, because the person who ends
        up looking at a screenshot of it will not have read technical-design.md.
        """
        lines = _banner(mock_settings, placeholder_calibration())
        joined = "\n".join(lines)
        assert MOCK_BANNER_MARKER in joined
        assert "NOT A DETECTOR" in joined
        assert "No model is loaded" in joined

    def test_real_mode_does_not_emit_the_mock_block(
        self, base_env: dict[str, str], fitted_calibration: Calibration
    ) -> None:
        """Prevents the warning becoming background noise that a reader learns to ignore."""
        env = dict(base_env)
        env["DETECTOR_MODE"] = "REAL_DETECTOR"
        lines = _banner(load_settings(env=env), fitted_calibration)
        assert MOCK_BANNER_MARKER not in "\n".join(lines)

    def test_placeholder_calibration_emits_a_probability_language_warning(
        self, mock_settings: ScorerSettings
    ) -> None:
        """rules.md R-11. Prevents ``spoof_risk`` being called a probability before it is one."""
        joined = "\n".join(_banner(mock_settings, placeholder_calibration()))
        assert "CALIBRATION STATUS PLACEHOLDER-NOT-POLICY-ELIGIBLE" in joined
        assert "must not be described as a probability" in joined

    def test_fitted_calibration_emits_no_calibration_warning(
        self, base_env: dict[str, str], fitted_calibration: Calibration
    ) -> None:
        env = dict(base_env)
        env["DETECTOR_MODE"] = "REAL_DETECTOR"
        joined = "\n".join(_banner(load_settings(env=env), fitted_calibration))
        assert "CALIBRATION STATUS" not in joined

    def test_non_policy_eligible_artifact_state_is_flagged(
        self, mock_settings: ScorerSettings
    ) -> None:
        joined = "\n".join(_banner(mock_settings, placeholder_calibration()))
        assert "NOT POLICY ELIGIBLE" in joined

    def test_unverified_contract_vector_parity_is_flagged(
        self, mock_settings: ScorerSettings
    ) -> None:
        """frame_contract.md §6. "Not checked" must look different from "checked and passed"."""
        joined = "\n".join(
            _banner(mock_settings, placeholder_calibration(), contract_vector_parity_ok=False)
        )
        assert "CONTRACT VECTOR PARITY NOT VERIFIED" in joined

    def test_verified_parity_emits_no_warning(
        self, base_env: dict[str, str], fitted_calibration: Calibration
    ) -> None:
        env = dict(base_env)
        env["DETECTOR_MODE"] = "REAL_DETECTOR"
        env["ARTIFACT_STATE"] = "policy_eligible"
        joined = "\n".join(
            _banner(
                load_settings(env=env),
                fitted_calibration,
                contract_vector_parity_ok=True,
                artifact_state=ArtifactState.POLICY_ELIGIBLE,
            )
        )
        assert "NOT VERIFIED" not in joined
        assert "NOT POLICY ELIGIBLE" not in joined
        assert "CAPPED" not in joined

    def test_capped_artifact_state_is_shown_alongside_the_declaration(
        self, base_env: dict[str, str]
    ) -> None:
        """Prevents a silent downgrade, which would look like a misconfiguration to whoever set it.

        Someone who declared ``demo_eligible`` and got ``research_only`` needs to see BOTH values to know
        the cap happened and that it was the artifacts, not a typo.
        """
        env = dict(base_env)
        env["ARTIFACT_STATE"] = "demo_eligible"
        joined = "\n".join(
            _banner(
                load_settings(env=env),
                placeholder_calibration(),
                artifact_state=ArtifactState.RESEARCH_ONLY,
            )
        )
        assert "CAPPED" in joined
        assert "declared: demo_eligible" in joined


class TestBannerParitySet:
    """architecture.md §5.1: both tiers must print the entire parity set at startup."""

    def test_every_parity_field_appears(self, mock_settings: ScorerSettings) -> None:
        """Prevents a field quietly dropping out, which would make the two tiers uncomparable by eye.

        The banner is what a human compares between the CPU and GPU tiers on Day 5. A missing line is a
        comparison nobody makes.
        """
        joined = "\n".join(_banner(mock_settings, placeholder_calibration(), model_sha256="b" * 64))
        for expected in (
            "deployment_profile",
            "execution_provider",
            "git_commit",
            "proto_contract",
            "window_contract",
            "model_version",
            "calibration_version",
            "calibration_method",
            "calibration_fitted_on",
            "detector_mode",
            "contract_vector_parity",
            "artifact_state",
        ):
            assert expected in joined, f"{expected} missing from the startup banner"

    def test_configured_and_actual_provider_are_both_shown(
        self, mock_settings: ScorerSettings
    ) -> None:
        """Prevents a fallback being invisible in the one artifact a human reads at startup.

        On the GPU tier a mismatch is fatal before the banner prints, so these will always agree in a
        healthy process. Printing both means the banner still states which was requested — so the record
        of intent survives even when someone reads only the log.
        """
        joined = "\n".join(_banner(mock_settings, placeholder_calibration()))
        assert "configured: CPUExecutionProvider" in joined

    def test_window_contract_numbers_are_printed(self, mock_settings: ScorerSettings) -> None:
        joined = "\n".join(_banner(mock_settings, placeholder_calibration()))
        assert "40960 samples" in joined
        assert "81920 bytes" in joined

    def test_banner_is_pure(self, mock_settings: ScorerSettings) -> None:
        """No clock, no I/O — so the content is assertable and identical across the two tiers."""
        first = _banner(mock_settings, placeholder_calibration())
        second = _banner(mock_settings, placeholder_calibration())
        assert first == second


@pytest.mark.privacy
class TestRedactingLogSink:
    """rules.md R-14 and R-17. The control is at the sink, so it catches call sites not yet written."""

    def _format(self, **extra: object) -> dict[str, object]:
        formatter = RedactingJsonFormatter(
            deployment_profile="local-cpu",
            git_commit="0000000",
            detector_mode="MOCK_SMOKE_MODE_NOT_A_DETECTOR",
        )
        record = logging.LogRecord(
            name="app.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="a message",
            args=(),
            exc_info=None,
        )
        for key, value in extra.items():
            setattr(record, key, value)
        return json.loads(formatter.format(record))

    def test_bytes_are_never_rendered(self) -> None:
        """Prevents ``logger.info("window=%s", pcm)`` ever putting audio in CloudWatch.

        This service handles an 81,920-byte audio payload on every request, and "log the buffer to debug
        the parser" is the single most likely way that audio escapes. The sink replaces any bytes value
        with its length, so the control applies to a call site written six months from now by someone who
        never read rules.md.
        """
        payload = b"\x01\x02" * 40_960
        rendered = self._format(bytes_actual=payload)
        assert "bytes withheld" in str(rendered["bytes_actual"])
        assert "\\x01" not in json.dumps(rendered)

    def test_bytes_inside_a_message_string_are_not_reachable(self) -> None:
        """A ``bytes`` value interpolated into the message is caught by the length substitution.

        The formatter scrubs the rendered message too, not just the structured extras — so an f-string is
        not a bypass.
        """
        formatter = RedactingJsonFormatter(
            deployment_profile="local-cpu", git_commit="0", detector_mode="REAL_DETECTOR"
        )
        record = logging.LogRecord(
            name="app.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="window=%s",
            args=(b"\x00" * 81_920,),
            exc_info=None,
        )
        rendered = json.loads(formatter.format(record))
        assert len(rendered["message"]) <= 2_100  # truncated, not a 160 KB log line

    def test_unknown_extra_keys_are_dropped_by_name_only(self) -> None:
        """Prevents a new field becoming a leak channel by default (allow-list, not deny-list).

        The key NAME is reported so a developer notices their field vanished; the value never is, which is
        safe even when the value was the thing that mattered.
        """
        rendered = self._format(caller_phone_number="+1 555 0100 0000")
        assert "caller_phone_number" not in rendered
        assert rendered["dropped_keys"] == ["caller_phone_number"]
        assert "555" not in json.dumps(rendered)

    def test_digit_runs_in_the_message_are_masked(self) -> None:
        """Defence in depth for R-15: a phone number that reaches a message string is still masked."""
        rendered = self._format()
        formatter = RedactingJsonFormatter(
            deployment_profile="local-cpu", git_commit="0", detector_mode="REAL_DETECTOR"
        )
        record = logging.LogRecord(
            name="app.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="reference 919876543210 rejected",
            args=(),
            exc_info=None,
        )
        masked = json.loads(formatter.format(record))
        assert "919876543210" not in masked["message"]
        assert "[redacted-digits]" in masked["message"]
        assert rendered["service"] == SERVICE_NAME

    def test_detector_mode_is_stamped_on_every_line(self) -> None:
        """rules.md R-46. Prevents a grep for a latency number finding a mock measurement unlabelled.

        Not only on lines that mention a score. Somebody investigating p95 will grep for a number, land on
        one line, and quote it. That line has to carry the label.
        """
        rendered = self._format()
        assert rendered["detector_mode"] == "MOCK_SMOKE_MODE_NOT_A_DETECTOR"

    def test_allow_list_contains_no_identity_or_content_key(self) -> None:
        """Prevents the Scorer's allow-list drifting toward the Gateway's, which has session identity.

        The Scorer has no session_id, no purpose_code, no risk_state, and no action. Enumerating keys it
        will never hold would invite someone to start holding them.
        """
        for forbidden in (
            "session_id",
            "purpose_code",
            "risk_state",
            "action",
            "phone",
            "msisdn",
            "pcm",
            "pcm_window",
            "audio",
            "transcript",
        ):
            assert forbidden not in ALLOWED_EXTRA_KEYS

    def test_call_ref_is_allowed_because_it_is_already_a_pseudonym(self) -> None:
        """rules.md R-16: ``session_ref`` arrives HMAC-pseudonymized. The Scorer never sees a raw number."""
        assert "call_ref" in ALLOWED_EXTRA_KEYS

    def test_a_bad_format_string_does_not_kill_the_process(self) -> None:
        """Prevents a logging bug taking down a service whose job is to keep scoring."""
        formatter = RedactingJsonFormatter(
            deployment_profile="local-cpu", git_commit="0", detector_mode="REAL_DETECTOR"
        )
        record = logging.LogRecord(
            name="app.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="%d %d",
            args=("not-an-int",),
            exc_info=None,
        )
        assert json.loads(formatter.format(record))["message"]

    def test_output_is_one_json_object_per_line(self) -> None:
        """Prevents a multi-line record breaking CloudWatch Logs Insights parsing of every later line."""
        formatter = RedactingJsonFormatter(
            deployment_profile="local-cpu", git_commit="0", detector_mode="REAL_DETECTOR"
        )
        record = logging.LogRecord(
            name="app.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="line one\nline two",
            args=(),
            exc_info=None,
        )
        rendered = formatter.format(record)
        assert "\n" not in rendered
        json.loads(rendered)


class TestLoggingConfiguration:
    """The redacting handler must be the ONLY handler, or every control above is bypassable."""

    def test_configure_logging_replaces_existing_handlers(
        self, mock_settings: ScorerSettings
    ) -> None:
        """Prevents a default-formatted second handler silently defeating the redaction.

        grpcio installs its own logging on first import. A second handler carrying a plain formatter would
        emit the unredacted record alongside the redacted one, and the log would contain both.
        """
        root = logging.getLogger()
        previous = list(root.handlers)
        previous_level = root.level
        root.addHandler(logging.StreamHandler())
        try:
            configure_logging(mock_settings)
            assert len(root.handlers) == 1
            assert isinstance(root.handlers[0].formatter, RedactingJsonFormatter)
        finally:
            for handler in list(root.handlers):
                root.removeHandler(handler)
            for handler in previous:
                root.addHandler(handler)
            root.setLevel(previous_level)

    def test_emit_banner_goes_through_the_sink(
        self, mock_settings: ScorerSettings, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Prevents the banner being printed on a path where the redaction does not apply.

        The banner prints artifact identity, and artifact identity is one field away from session identity.
        An unredacted banner path would itself be the leak path.
        """
        lines = _banner(mock_settings, placeholder_calibration())
        with caplog.at_level(logging.INFO):
            emit_banner(lines, logging.getLogger("app.banner.test"))
        assert len(caplog.records) == len(lines)
        assert all(record.component == "banner" for record in caplog.records)

    def test_banner_uses_the_logger_not_print(self) -> None:
        """Prevents the banner going to stdout while the logs go elsewhere.

        On ECS the log driver is what reaches CloudWatch. A banner printed outside it is a banner nobody
        sees on the tier where it matters most.
        """
        from app import banner as banner_module

        source = Path(banner_module.__file__).read_text(encoding="utf-8")
        assert "logger.info(line" in source
        assert "print(" not in source


def test_no_test_in_this_file_contains_a_plausible_secret() -> None:
    """rules.md R-34, asserted about this file itself.

    The redaction tests need realistic-looking inputs, and a realistic-looking input is exactly what a
    secret scanner flags — and what someone copies into a real config. So the inputs are a phone-shaped
    digit run and an obvious marker string, never a token- or key-shaped value.

    The patterns are assembled from fragments so that this assertion does not put the very strings it
    forbids into the file it is scanning.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    for pattern in ("AKI" + "A", "BEGIN PRIVATE " + "KEY", "Bearer " + "ey", "pass" + "word="):
        assert pattern not in source
