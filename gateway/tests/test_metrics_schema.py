"""Tests for gateway/app/telemetry/metrics.py.

Asserts metric schema integrity, duplicate prevention, allowed labels, and privacy boundary
invariants (rules.md R-03, R-15, R-53).
"""

from __future__ import annotations

import dataclasses

import pytest

from app.telemetry.metrics import (
    ALLOWED_LABELS,
    METRIC_SCHEMA_VERSION,
    METRICS,
    MetricDef,
    MetricKind,
    validate_definitions,
)


def test_metric_schema_version_is_v1() -> None:
    assert METRIC_SCHEMA_VERSION == "v1"


def test_validate_definitions_passes_on_committed_metrics() -> None:
    """The default metric set must be fully valid and consistent."""
    validate_definitions()


def test_metrics_tuple_is_non_empty_and_contains_metric_defs() -> None:
    assert len(METRICS) > 0
    for m in METRICS:
        assert isinstance(m, MetricDef)
        assert isinstance(m.name, str) and len(m.name) > 0
        assert isinstance(m.kind, MetricKind)
        assert isinstance(m.unit, str) and len(m.unit) > 0
        assert isinstance(m.description, str) and len(m.description) > 0
        assert isinstance(m.labels, tuple)


def test_metric_def_is_immutable() -> None:
    m = METRICS[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.name = "altered_metric_name"  # type: ignore[misc]


def test_metric_names_are_unique() -> None:
    names = [m.name for m in METRICS]
    assert len(names) == len(set(names))


def test_metric_names_follow_snake_case() -> None:
    for m in METRICS:
        assert m.name.islower()
        assert not m.name.startswith("_")
        assert not m.name.endswith("_")


def test_validate_definitions_raises_on_duplicate_name(monkeypatch: pytest.MonkeyPatch) -> None:
    dup = MetricDef(
        name=METRICS[0].name,
        kind=MetricKind.COUNTER,
        unit="events",
        description="Duplicate",
        labels=("deployment_profile",),
    )
    fake_metrics = METRICS + (dup,)
    monkeypatch.setattr("app.telemetry.metrics.METRICS", fake_metrics)
    with pytest.raises(ValueError, match="duplicate metric name"):
        validate_definitions()


@pytest.mark.parametrize(
    "forbidden_label",
    [
        "call_ref",
        "session_id",
        "speaker_id",
        "user_id",
        "phone_number",
        "customer_name",
        "raw_audio",
        "audio_bytes",
    ],
)
def test_validate_definitions_rejects_forbidden_or_pii_labels(
    forbidden_label: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Privacy boundary: per-caller/PII labels must never enter metrics (rules.md R-03, R-15)."""
    assert forbidden_label not in ALLOWED_LABELS
    bad_metric = MetricDef(
        name="test_privacy_leak_metric",
        kind=MetricKind.COUNTER,
        unit="events",
        description="Bad label",
        labels=(forbidden_label,),
    )
    fake_metrics = METRICS + (bad_metric,)
    monkeypatch.setattr("app.telemetry.metrics.METRICS", fake_metrics)
    with pytest.raises(ValueError, match="labels not permitted"):
        validate_definitions()


def test_expected_core_metrics_are_present() -> None:
    """Core metric inventory defined in metrics.py."""
    names = {m.name for m in METRICS}
    expected = {
        "gateway_frames_received_total",
        "gateway_frames_rejected_total",
        "gateway_frames_discarded_unvoiced_total",
        "gateway_windows_scored_total",
        "gateway_windows_dropped_total",
        "gateway_first_decision_latency_ms",
        "gateway_scorer_latency_us",
        "gateway_policy_actions_total",
        "gateway_live_streams",
        "gateway_backpressure_rejections_total",
        "audit_events_written_total",
        "audit_write_failures_total",
        "audit_hash_verification_failures",
    }
    assert expected == names


def test_allowed_labels_contains_only_low_cardinality_operational_dimensions() -> None:
    assert ALLOWED_LABELS == {
        "deployment_profile",
        "execution_provider",
        "detector_mode",
        "purpose_code",
        "risk_state",
        "action",
        "reason_code",
        "code",
        "eligible",
    }
