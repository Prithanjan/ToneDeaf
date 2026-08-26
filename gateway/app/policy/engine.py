"""Policy state machine — PURE, no I/O (rules.md R-53).

The decision half of the detection/decision seam. The Scorer produces a number; THIS produces the
action. Everything that makes the decision defensible lives here as a pure function of an ordered
score sequence, so the whole behaviour is reproducible from a list of floats in a unit test.

Four invariants, each of which exists because a source document made it a stop condition:

* **The action vocabulary is closed** — ``continue`` / ``verify`` / ``hold`` / ``escalate``.
  ``approve`` and ``deny`` are absent by construction (rules.md R-07): there is no enum member to
  return, no mapping key that accepts one, and a policy bundle containing one fails to load. Adding
  an authorization outcome is therefore not a one-line change.
* **One high window never triggers a high-risk action** — the k-of-n rule is the evidence bar
  (rules.md R-08). ``k`` is loaded from the policy bundle, and the loader refuses ``k < 2``.
* **Ineligible windows are skipped, never counted as low risk** (rules.md R-09). A codec-degraded
  window is absence of evidence, not evidence of absence.
* **``high`` is sticky** for the session (rules.md R-13). Evidence does not evaporate because the
  next window looked clean; clearing it requires an explicit human resolution step (Phase 4).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum


class RiskState(str, Enum):
    COLLECTING = "collecting"
    UNCERTAIN = "uncertain"
    HIGH = "high"


class Action(str, Enum):
    """The complete action vocabulary.

    Do not add ``APPROVE`` or ``DENY``. This system produces proportionate verification pressure;
    it never issues an authorization outcome (rules.md R-07). The five-day plan lists adding one as
    a stop condition, and the CI policy-lint test asserts this enum's members exactly.
    """

    CONTINUE = "continue"
    VERIFY = "verify"
    HOLD = "hold"
    ESCALATE = "escalate"


class ReasonCode(str, Enum):
    """Why this action, in a form the Privacy Inspector can render to a human."""

    EVIDENCE_BELOW_K = "EVIDENCE_BELOW_K"
    EVIDENCE_K_OF_N_MET = "EVIDENCE_K_OF_N_MET"
    INSUFFICIENT_ELIGIBLE_WINDOWS = "INSUFFICIENT_ELIGIBLE_WINDOWS"
    QUALITY_DEGRADED = "QUALITY_DEGRADED"


@dataclass(frozen=True, slots=True)
class PolicyThresholds:
    """Evidence parameters, loaded from ``policy/policy.yaml``.

    ``high_window_risk`` is currently ``0.78`` carrying ``derivation: placeholder`` (decision
    D-11). The playbook is explicit that there is no universally valid 0.78; it must be re-derived
    from a cost-sensitive matrix before any release claims ``policy_eligible``.
    """

    high_window_risk: float
    evidence_k: int
    evidence_n: int

    def __post_init__(self) -> None:
        if not 0.0 < self.high_window_risk < 1.0:
            raise ValueError("high_window_risk must be in (0, 1)")
        if self.evidence_k < 2:
            # Guards rules.md R-08 at load time. "Lower k to 1 for a more responsive demo" is the
            # specific failure this refuses, and it refuses it before any audio arrives.
            raise ValueError("evidence_k must be >= 2: one high window may never decide")
        if self.evidence_n < self.evidence_k:
            raise ValueError("evidence_n must be >= evidence_k")


@dataclass(frozen=True, slots=True)
class WindowObservation:
    """One scored window, as the engine sees it.

    Deliberately does NOT carry audio, features, ``raw_score``, or a timestamp. ``raw_score`` is
    diagnostics-only and reading it here would violate rules.md R-11.
    """

    window_seq: int
    spoof_risk: float
    eligible: bool
    quality_flagged: bool = False


@dataclass(frozen=True, slots=True)
class Decision:
    """The engine's complete output. No audio, no features, no free text."""

    risk_state: RiskState
    action: Action
    reason_code: ReasonCode
    eligible_window_count: int
    high_window_count: int
    state_changed: bool


PurposeActionMap = dict[str, dict[RiskState, Action]]


@dataclass(slots=True)
class PolicyEngine:
    """Per-session evidence accumulator. One instance per stream; never shared.

    Stateful by nature — it is the session's memory — but every transition is a deterministic
    function of ``(thresholds, purpose_code, observation sequence)``, so a failing demo can be
    replayed exactly from the audit table's ``spoof_risk`` column.
    """

    thresholds: PolicyThresholds
    purpose_code: str
    purpose_actions: PurposeActionMap

    _recent: deque[bool] = field(init=False, repr=False)
    _eligible_seen: int = field(default=0, init=False)
    _state: RiskState = field(default=RiskState.COLLECTING, init=False)

    def __post_init__(self) -> None:
        if self.purpose_code not in self.purpose_actions:
            raise ValueError(f"no action mapping for purpose_code: {self.purpose_code!r}")
        self._recent = deque(maxlen=self.thresholds.evidence_n)

    # -- state -----------------------------------------------------------------------------------

    @property
    def state(self) -> RiskState:
        return self._state

    def action_for(self, state: RiskState) -> Action:
        return self.purpose_actions[self.purpose_code][state]

    # -- transition ------------------------------------------------------------------------------

    def observe(self, obs: WindowObservation) -> Decision:
        """Fold one scored window into the session's evidence and return the current decision."""
        previous = self._state

        if not obs.eligible:
            # Skipped, NOT counted as low risk (rules.md R-09). The window never enters the
            # k-of-n deque at all, so it can neither raise nor dilute the evidence count.
            reason = (
                ReasonCode.QUALITY_DEGRADED
                if obs.quality_flagged
                else ReasonCode.INSUFFICIENT_ELIGIBLE_WINDOWS
            )
            return self._decision(reason, state_changed=False)

        self._eligible_seen += 1
        self._recent.append(obs.spoof_risk >= self.thresholds.high_window_risk)

        if self._state is RiskState.HIGH:
            # Sticky (rules.md R-13). Still recorded above so the audit trail shows the full
            # evidence sequence rather than going quiet after the trigger.
            return self._decision(ReasonCode.EVIDENCE_K_OF_N_MET, state_changed=False)

        if len(self._recent) < self.thresholds.evidence_n:
            self._state = RiskState.COLLECTING
            return self._decision(
                ReasonCode.INSUFFICIENT_ELIGIBLE_WINDOWS,
                state_changed=previous is not self._state,
            )

        high = sum(self._recent)
        if high >= self.thresholds.evidence_k:
            self._state = RiskState.HIGH
            reason = ReasonCode.EVIDENCE_K_OF_N_MET
        else:
            self._state = RiskState.UNCERTAIN
            reason = ReasonCode.EVIDENCE_BELOW_K

        return self._decision(reason, state_changed=previous is not self._state)

    def _decision(self, reason: ReasonCode, *, state_changed: bool) -> Decision:
        return Decision(
            risk_state=self._state,
            action=self.action_for(self._state),
            reason_code=reason,
            eligible_window_count=self._eligible_seen,
            high_window_count=sum(self._recent),
            state_changed=state_changed,
        )


def replay(
    thresholds: PolicyThresholds,
    purpose_code: str,
    purpose_actions: PurposeActionMap,
    observations: list[WindowObservation],
) -> list[Decision]:
    """Run a full observation sequence through a fresh engine.

    The Phase-2 policy-sequence tests and the Phase-4 temporal-regression suite both drive the
    engine through this, which is why the engine takes no clock and no I/O: a regression is a diff
    between two lists, not a re-run of the stack.
    """
    engine = PolicyEngine(thresholds, purpose_code, purpose_actions)
    return [engine.observe(o) for o in observations]
