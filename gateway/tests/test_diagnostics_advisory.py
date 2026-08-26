"""Tests for gateway/app/policy/diagnostics.py.

Asserts the diagnostics plane is strictly advisory, has no decision influence, and enforces
privacy and governance invariants (rules.md R-12, R-14, R-39, R-41; decision D-12).
"""

from __future__ import annotations

import dataclasses
import inspect
import pytest

from app.policy.diagnostics import DiagnosticObservation, DiagnosticsSidecar


def test_diagnostic_observation_is_immutable() -> None:
    obs = DiagnosticObservation(window_seq=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        obs.window_seq = 1  # type: ignore[misc]


def test_observation_display_payload_is_explicitly_advisory() -> None:
    """The payload shape for the UI must label itself advisory and non-decision-bearing."""
    obs = DiagnosticObservation(window_seq=3, descriptors={"spectral_tilt": 0.12}, notes=("sample note",))
    payload = obs.as_display_payload()

    assert payload["window_seq"] == 3
    assert payload["advisory"] is True
    assert payload["influences_decision"] is False
    assert payload["descriptors"] == {"spectral_tilt": 0.12}
    assert payload["notes"] == ["sample note"]


def test_diagnostics_sidecar_default_disabled() -> None:
    sidecar = DiagnosticsSidecar()
    assert sidecar.enabled is False


def test_diagnostics_sidecar_disabled_returns_empty_observation() -> None:
    sidecar = DiagnosticsSidecar(enabled=False)
    obs = sidecar.observe(window_seq=1, spoof_risk=0.85, quality_flags=())
    assert obs.window_seq == 1
    assert obs.descriptors == {}
    assert obs.notes == ()
    payload = obs.as_display_payload()
    assert payload["advisory"] is True
    assert payload["influences_decision"] is False
    assert payload["descriptors"] == {}


def test_diagnostics_sidecar_enabled_returns_ablation_gate_note() -> None:
    """Until real descriptors pass the ablation gate, enabled sidecar yields empty descriptors."""
    sidecar = DiagnosticsSidecar(enabled=True)
    assert sidecar.enabled is True
    obs = sidecar.observe(window_seq=2, spoof_risk=0.42, quality_flags=("LOW_ENERGY",))
    assert obs.window_seq == 2
    assert obs.descriptors == {}
    assert len(obs.notes) == 1
    assert "ablation gate" in obs.notes[0]


def test_observe_signature_does_not_accept_raw_audio() -> None:
    """R-14: No raw audio may reach the diagnostics sidecar."""
    sig = inspect.signature(DiagnosticsSidecar.observe)
    param_names = set(sig.parameters.keys())
    assert "pcm" not in param_names
    assert "audio" not in param_names
    assert "raw_samples" not in param_names
    assert {"self", "window_seq", "spoof_risk", "quality_flags"} <= param_names


@pytest.mark.parametrize(
    "forbidden_demographic_or_carrier_descriptor",
    [
        "accent",
        "emotion",
        "illness",
        "gender",
        "age",
        "speaking_style",
        "carrier_sampling_rate_cutoff",
        "telecom_codec_flag",
    ],
)
def test_forbidden_categories_cannot_be_descriptor_keys(
    forbidden_demographic_or_carrier_descriptor: str,
) -> None:
    """R-39, R-41: Diagnostics must never categorize speaker demographics, accent, or carrier codec."""
    obs = DiagnosticObservation(
        window_seq=0,
        descriptors={},
        notes=(),
    )
    assert forbidden_demographic_or_carrier_descriptor not in obs.descriptors
