"""Diagnostics sidecar — advisory only, and structurally unable to be otherwise (decision D-12).

The playbook's decision rule for the diagnostics plane is that descriptors may be computed and
displayed, but must not influence a decision until an ablation gate demonstrates they add value
beyond the primary model (rules.md R-12).

"Advisory only" is a promise. This module makes it a code property: :func:`observe` returns a
:class:`DiagnosticObservation`, and the policy engine's call site **discards the return value**. To
wire diagnostics into a decision you would have to change the call site, which is a visible diff in
a reviewed file rather than a threshold nudged in a config.

Explicitly forbidden here, and asserted by ``gateway/tests/test_diagnostics_advisory.py``:

* No frequency boundary, sampling rate, or spectral cutoff is a spoof rule. Sampling rate is a
  channel characteristic (rules.md R-39) — an 8 kHz/16 kHz split describes the carrier, not the
  speaker, and using it as evidence would systematically penalize callers on older networks.
* Nothing here may describe accent, emotion, illness, gender, age, or speaking style
  (rules.md R-41).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class DiagnosticObservation:
    """Descriptive observations about a window. NOT evidence, NOT a score, NOT a policy input.

    Every field describes the *channel or the recording*, never the person speaking.
    """

    window_seq: int
    #: Named descriptor -> value. Named observations only; no aggregate "suspicion" number, because
    #: a single number is exactly the thing that gets quietly promoted to a threshold.
    descriptors: dict[str, float] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def as_display_payload(self) -> dict[str, object]:
        """Shape for the Phase-4 Privacy Inspector.

        Labelled advisory at the boundary, so a UI cannot render it as a verdict by omission.
        """
        return {
            "window_seq": self.window_seq,
            "advisory": True,
            "influences_decision": False,
            "descriptors": dict(self.descriptors),
            "notes": list(self.notes),
        }


class DiagnosticsSidecar:
    """Phase-1 interface with a deliberately empty implementation.

    The interface exists now so the seam is fixed before there is any pressure to add a descriptor
    mid-demo. The implementation stays empty until the ablation gate in the playbook passes; landing
    real descriptors is a Phase-4 task with an evaluation report attached, not a Day-4 improvisation.
    """

    __slots__ = ("_enabled",)

    def __init__(self, *, enabled: bool = False):
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    def observe(
        self, *, window_seq: int, spoof_risk: float, quality_flags: tuple[str, ...]
    ) -> DiagnosticObservation:
        """Compute advisory descriptors for one window.

        Takes ``spoof_risk`` and ``quality_flags`` rather than PCM on purpose: a sidecar holding raw
        audio would be a second copy of the thing rules.md R-14 says exists in exactly one place.
        When real descriptors land, they are computed inside the Scorer — where the window already
        is — and passed here as numbers.
        """
        if not self._enabled:
            return DiagnosticObservation(window_seq=window_seq)
        return DiagnosticObservation(
            window_seq=window_seq,
            descriptors={},
            notes=("diagnostics enabled but no descriptor has passed the ablation gate",),
        )
