"""Metric definitions — a versioned schema, not ad-hoc counter names.

Metric names and label sets are declared here because the five-day plan requires every capability
claim to map to recorded evidence (rules.md R-03). A metric invented inline on Day 4 has no
definition, no owner, and no way to be compared against Day 2's number.

**Label cardinality is a privacy control as much as a cost control.** ``call_ref`` is deliberately NOT
a label anywhere: a per-caller time series is a behavioural record of individuals, which is exactly
what the feature-only audit boundary exists to avoid, and CloudWatch dimensions are not covered by the
audit table's deny-list test. Session-scoped facts belong in the audit trail, which has retention and
a deletion path; metrics carry aggregates only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final

METRIC_SCHEMA_VERSION: Final[str] = "v1"


class MetricKind(str, Enum):
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


@dataclass(frozen=True, slots=True)
class MetricDef:
    name: str
    kind: MetricKind
    unit: str
    description: str
    labels: tuple[str, ...] = ()


#: Labels permitted on ANY metric. Every one is low-cardinality and describes the deployment or the
#: decision, never a person. Adding a label here is a reviewed change.
ALLOWED_LABELS: Final[frozenset[str]] = frozenset(
    {"deployment_profile", "execution_provider", "detector_mode", "purpose_code", "risk_state",
     "action", "reason_code", "code", "eligible"}
)

METRICS: Final[tuple[MetricDef, ...]] = (
    MetricDef(
        "gateway_frames_received_total", MetricKind.COUNTER, "frames",
        "648-byte frames accepted from clients.",
        ("deployment_profile",),
    ),
    MetricDef(
        "gateway_frames_rejected_total", MetricKind.COUNTER, "frames",
        "Frames rejected by the contract check. Never coerced (rules.md R-24).",
        ("deployment_profile", "code"),
    ),
    MetricDef(
        "gateway_frames_discarded_unvoiced_total", MetricKind.COUNTER, "frames",
        "Frames the VAD gated out. Silence is discarded, never scored.",
        ("deployment_profile",),
    ),
    MetricDef(
        "gateway_windows_scored_total", MetricKind.COUNTER, "windows",
        "2.56 s voiced windows successfully scored.",
        ("deployment_profile", "eligible"),
    ),
    MetricDef(
        "gateway_windows_dropped_total", MetricKind.COUNTER, "windows",
        "Windows dropped because the Scorer was unavailable. NOT counted as low risk.",
        ("deployment_profile",),
    ),
    MetricDef(
        "gateway_first_decision_latency_ms", MetricKind.HISTOGRAM, "ms",
        "Wall-clock from session.accepted to the first policy.action. Expected to exceed 2560 ms: "
        "the first window needs 2.56 s of VOICED audio, which is more than 2.56 s of wall clock.",
        ("deployment_profile", "purpose_code"),
    ),
    MetricDef(
        "gateway_scorer_latency_us", MetricKind.HISTOGRAM, "us",
        "Scorer-reported inference latency. A p95 belongs to a named host (rules.md R-47).",
        ("deployment_profile", "execution_provider"),
    ),
    MetricDef(
        "gateway_policy_actions_total", MetricKind.COUNTER, "actions",
        "Actions emitted. Label set contains exactly continue|verify|hold|escalate (rules.md R-07).",
        ("deployment_profile", "purpose_code", "risk_state", "action", "reason_code"),
    ),
    MetricDef(
        "gateway_live_streams", MetricKind.GAUGE, "streams",
        "Concurrent live streams. Bounded; excess is refused, never queued (rules.md R-20).",
        ("deployment_profile",),
    ),
    MetricDef(
        "gateway_backpressure_rejections_total", MetricKind.COUNTER, "streams",
        "Streams refused at capacity. A non-zero value is correct behaviour, not an error.",
        ("deployment_profile",),
    ),
    MetricDef(
        "audit_events_written_total", MetricKind.COUNTER, "events",
        "Chained audit rows written.",
        ("deployment_profile",),
    ),
    MetricDef(
        "audit_write_failures_total", MetricKind.COUNTER, "events",
        "Failed audit inserts. Must be 0 for a release: a missing row is a hole in the chain.",
        ("deployment_profile",),
    ),
    MetricDef(
        "audit_hash_verification_failures", MetricKind.GAUGE, "sessions",
        "Sessions whose chain failed verification. Must be 0 (technical-design.md section 5.3).",
        ("deployment_profile",),
    ),
)


def validate_definitions() -> None:
    """Assert the schema is internally consistent. Called by ``gateway/tests/test_metrics_schema.py``.

    The label check is the substance: it fails on any label outside :data:`ALLOWED_LABELS`, which is
    how a well-meaning ``call_ref`` dimension gets caught in CI rather than in a CloudWatch bill and a
    privacy review.
    """
    seen: set[str] = set()
    for metric in METRICS:
        if metric.name in seen:
            raise ValueError(f"duplicate metric name: {metric.name}")
        seen.add(metric.name)
        bad = set(metric.labels) - ALLOWED_LABELS
        if bad:
            raise ValueError(f"{metric.name}: labels not permitted: {sorted(bad)}")


validate_definitions()
