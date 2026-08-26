"""Policy state-machine tests.

This is where the product claim is either true or false. Every test below corresponds to a stop
condition in the source documents: if one of these fails, the system is making an authorization
decision, or deciding on one window, or treating a degraded window as clean — and the demo is
misrepresenting what it does.
"""

from __future__ import annotations

import pytest

from app.policy.engine import (
    Action,
    Decision,
    PolicyEngine,
    PolicyThresholds,
    ReasonCode,
    RiskState,
    WindowObservation,
    replay,
)

THRESHOLDS = PolicyThresholds(high_window_risk=0.78, evidence_k=3, evidence_n=5)

#: The Phase-1 placeholder mapping. Mirrors policy/policy.yaml's shape, not its authority.
ACTIONS = {
    "payment_authorization": {
        RiskState.COLLECTING: Action.CONTINUE,
        RiskState.UNCERTAIN: Action.VERIFY,
        RiskState.HIGH: Action.HOLD,
    },
    "account_recovery": {
        RiskState.COLLECTING: Action.CONTINUE,
        RiskState.UNCERTAIN: Action.VERIFY,
        RiskState.HIGH: Action.ESCALATE,
    },
}

HIGH = 0.95
LOW = 0.10


def engine(
    purpose: str = "payment_authorization", thresholds: PolicyThresholds = THRESHOLDS
) -> PolicyEngine:
    return PolicyEngine(thresholds, purpose, ACTIONS)  # type: ignore[arg-type]


def observe(eng: PolicyEngine, risks: list[float], *, eligible: bool = True) -> list[Decision]:
    return [
        eng.observe(WindowObservation(window_seq=i, spoof_risk=r, eligible=eligible))
        for i, r in enumerate(risks)
    ]


class TestActionVocabulary:
    def test_vocabulary_is_exactly_four_actions(self) -> None:
        """rules.md R-07. Asserted as an exact set, not a subset: a subset check would pass after
        someone added APPROVE."""
        assert {a.value for a in Action} == {"continue", "verify", "hold", "escalate"}

    @pytest.mark.parametrize("forbidden", ["APPROVE", "DENY", "ALLOW", "BLOCK", "REJECT"])
    def test_authorization_outcomes_do_not_exist(self, forbidden: str) -> None:
        """The system produces proportionate verification pressure, never an authorization outcome.
        There is no member to return, so adding one is a reviewed change to this enum — not a config
        value someone can set under demo pressure."""
        assert not hasattr(Action, forbidden)

    def test_no_state_maps_to_anything_outside_the_vocabulary(self) -> None:
        for mapping in ACTIONS.values():
            assert set(mapping) == set(RiskState)
            assert all(isinstance(a, Action) for a in mapping.values())


class TestEvidenceBar:
    def test_one_high_window_does_not_reach_high(self) -> None:
        """rules.md R-08, the single most important behavioural test in the suite."""
        decisions = observe(engine(), [HIGH])
        assert decisions[-1].risk_state is RiskState.COLLECTING
        assert decisions[-1].action is Action.CONTINUE

    def test_two_high_windows_do_not_reach_high_when_k_is_three(self) -> None:
        decisions = observe(engine(), [HIGH, HIGH])
        assert decisions[-1].risk_state is not RiskState.HIGH

    def test_three_of_five_reaches_high(self) -> None:
        decisions = observe(engine(), [HIGH, LOW, HIGH, LOW, HIGH])
        assert decisions[-1].risk_state is RiskState.HIGH
        assert decisions[-1].reason_code is ReasonCode.EVIDENCE_K_OF_N_MET
        assert decisions[-1].high_window_count == 3

    def test_two_of_five_is_uncertain_not_high(self) -> None:
        decisions = observe(engine(), [HIGH, LOW, HIGH, LOW, LOW])
        assert decisions[-1].risk_state is RiskState.UNCERTAIN
        assert decisions[-1].reason_code is ReasonCode.EVIDENCE_BELOW_K
        assert decisions[-1].action is Action.VERIFY

    def test_no_decision_before_n_windows(self) -> None:
        """Until the window is full the state is collecting — not "uncertain by default"."""
        decisions = observe(engine(), [HIGH] * 4)
        assert all(d.risk_state is RiskState.COLLECTING for d in decisions)
        assert decisions[-1].reason_code is ReasonCode.INSUFFICIENT_ELIGIBLE_WINDOWS

    @pytest.mark.parametrize("k", [0, 1, -1])
    def test_k_below_two_is_refused_at_construction(self, k: int) -> None:
        """ "Lower k to 1 so the demo triggers faster" is refused before any audio arrives."""
        with pytest.raises(ValueError, match="one high window may never decide"):
            PolicyThresholds(high_window_risk=0.78, evidence_k=k, evidence_n=5)

    def test_n_below_k_is_refused(self) -> None:
        with pytest.raises(ValueError):
            PolicyThresholds(high_window_risk=0.78, evidence_k=5, evidence_n=3)

    @pytest.mark.parametrize("threshold", [0.0, 1.0, -0.1, 1.5])
    def test_threshold_must_be_a_strict_probability(self, threshold: float) -> None:
        with pytest.raises(ValueError):
            PolicyThresholds(high_window_risk=threshold, evidence_k=3, evidence_n=5)


class TestThresholdBoundary:
    def test_exactly_at_threshold_counts_as_high(self) -> None:
        """>= is the documented comparison. Pinned because a later >  would shift every reported
        rate by an amount nobody would think to look for."""
        decisions = observe(engine(), [0.78] * 5)
        assert decisions[-1].risk_state is RiskState.HIGH

    def test_just_below_threshold_does_not(self) -> None:
        decisions = observe(engine(), [0.7799] * 5)
        assert decisions[-1].risk_state is RiskState.UNCERTAIN


class TestIneligibleWindows:
    def test_ineligible_windows_are_skipped_not_counted_low(self) -> None:
        """rules.md R-09. A codec-degraded window is absence of evidence, not evidence of absence.

        If ineligible windows diluted the deque, an attacker could suppress a decision by degrading
        audio quality — which is a cheaper attack than defeating the detector. Here 20 ineligible
        windows are interposed mid-evidence: the eligible sequence [H,H,H,L,L] must reach HIGH exactly
        as it would have without them, and the eligible count must still be 5.
        """
        eng = engine()
        observe(eng, [HIGH, HIGH, HIGH, LOW], eligible=True)
        observe(eng, [LOW] * 20, eligible=False)
        final = observe(eng, [LOW], eligible=True)[-1]
        assert final.risk_state is RiskState.HIGH
        assert final.reason_code is ReasonCode.EVIDENCE_K_OF_N_MET
        assert final.eligible_window_count == 5
        assert final.high_window_count == 3

    def test_interposed_ineligible_windows_do_not_evict_evidence(self) -> None:
        """The deque is bounded at n=5 ELIGIBLE windows. Ineligible ones must not consume a slot, or
        a burst of degraded audio would push real high-risk evidence out of the window."""
        eng = engine()
        observe(eng, [HIGH, HIGH, HIGH], eligible=True)
        observe(eng, [HIGH] * 10, eligible=False)
        final = observe(eng, [LOW, LOW], eligible=True)[-1]
        assert final.high_window_count == 3
        assert final.risk_state is RiskState.HIGH

    def test_ineligible_window_does_not_advance_the_eligible_count(self) -> None:
        eng = engine()
        decision = eng.observe(WindowObservation(window_seq=0, spoof_risk=HIGH, eligible=False))
        assert decision.eligible_window_count == 0
        assert decision.high_window_count == 0

    def test_quality_flagged_ineligible_window_reports_its_reason(self) -> None:
        eng = engine()
        decision = eng.observe(
            WindowObservation(window_seq=0, spoof_risk=LOW, eligible=False, quality_flagged=True)
        )
        assert decision.reason_code is ReasonCode.QUALITY_DEGRADED

    def test_unflagged_ineligible_window_reports_insufficiency(self) -> None:
        eng = engine()
        decision = eng.observe(WindowObservation(window_seq=0, spoof_risk=LOW, eligible=False))
        assert decision.reason_code is ReasonCode.INSUFFICIENT_ELIGIBLE_WINDOWS

    def test_an_all_ineligible_session_never_leaves_collecting(self) -> None:
        eng = engine()
        decisions = observe(eng, [HIGH] * 50, eligible=False)
        assert all(d.risk_state is RiskState.COLLECTING for d in decisions)
        assert all(d.action is Action.CONTINUE for d in decisions)


class TestStickiness:
    def test_high_does_not_decay(self) -> None:
        """rules.md R-13. Evidence does not evaporate because the next window looked clean."""
        eng = engine()
        observe(eng, [HIGH, HIGH, HIGH, LOW, LOW])
        assert eng.state is RiskState.HIGH
        after = observe(eng, [LOW] * 20)
        assert all(d.risk_state is RiskState.HIGH for d in after)
        assert eng.state is RiskState.HIGH

    def test_sticky_high_still_records_subsequent_evidence(self) -> None:
        """The audit trail must show the full sequence rather than going quiet after the trigger."""
        eng = engine()
        observe(eng, [HIGH] * 5)
        low_run = observe(eng, [LOW] * 5)
        assert low_run[-1].high_window_count == 0  # deque rolled over; state is still HIGH
        assert low_run[-1].risk_state is RiskState.HIGH

    def test_state_changed_fires_once_per_transition(self) -> None:
        eng = engine()
        decisions = observe(eng, [HIGH] * 5 + [HIGH] * 3)
        assert sum(1 for d in decisions if d.state_changed) == 1
        assert decisions[4].state_changed is True


class TestPurposeSensitivity:
    def test_same_evidence_different_purpose_different_action(self) -> None:
        """The purpose-sensitivity claim, expressed as a test: identical scores, different actions.

        The Scorer produced the same number in both cases; only the decision layer differs. That is
        the detection/decision separation being visible in behaviour rather than in a diagram.
        """
        risks = [HIGH, HIGH, HIGH, LOW, LOW]
        payment = observe(engine("payment_authorization"), risks)[-1]
        recovery = observe(engine("account_recovery"), risks)[-1]
        assert payment.risk_state is recovery.risk_state is RiskState.HIGH
        assert payment.action is Action.HOLD
        assert recovery.action is Action.ESCALATE

    def test_unmapped_purpose_is_refused_at_construction(self) -> None:
        """Not defaulted to the most permissive action — refused. A missing mapping is a policy gap,
        and defaulting one silently would answer a question nobody decided."""
        with pytest.raises(ValueError, match="no action mapping"):
            PolicyEngine(THRESHOLDS, "unknown_purpose", ACTIONS)  # type: ignore[arg-type]


class TestObservationShape:
    @pytest.mark.privacy
    def test_observation_cannot_carry_raw_score_or_audio(self) -> None:
        """rules.md R-11: raw_score is diagnostics-only, and the engine must not be able to read it
        even by accident. Asserted as an exact field set."""
        assert set(WindowObservation.__dataclass_fields__) == {
            "window_seq",
            "spoof_risk",
            "eligible",
            "quality_flagged",
        }

    @pytest.mark.privacy
    def test_decision_carries_no_free_text(self) -> None:
        """A free-text field on the decision is where a caller reference eventually gets rendered."""
        assert set(Decision.__dataclass_fields__) == {
            "risk_state",
            "action",
            "reason_code",
            "eligible_window_count",
            "high_window_count",
            "state_changed",
        }


class TestReplay:
    def test_replay_is_deterministic(self) -> None:
        """The whole reason the engine takes no clock and no I/O: a temporal regression is a diff
        between two lists, not a re-run of the stack (technical-design.md section 9)."""
        observations = [
            WindowObservation(window_seq=i, spoof_risk=r, eligible=e)
            for i, (r, e) in enumerate(
                [(HIGH, True), (LOW, False), (HIGH, True), (LOW, True), (HIGH, True), (LOW, True)]
            )
        ]
        first = replay(THRESHOLDS, "payment_authorization", ACTIONS, observations)  # type: ignore[arg-type]
        second = replay(THRESHOLDS, "payment_authorization", ACTIONS, observations)  # type: ignore[arg-type]
        assert first == second

    def test_replay_matches_an_incremental_engine(self) -> None:
        risks = [HIGH, LOW, HIGH, LOW, HIGH, LOW, LOW]
        observations = [WindowObservation(i, r, True) for i, r in enumerate(risks)]
        assert replay(THRESHOLDS, "payment_authorization", ACTIONS, observations) == observe(  # type: ignore[arg-type]
            engine(), risks
        )

    def test_replay_starts_from_a_fresh_state(self) -> None:
        observations = [WindowObservation(i, HIGH, True) for i in range(5)]
        assert replay(THRESHOLDS, "payment_authorization", ACTIONS, observations)[0].risk_state is (  # type: ignore[arg-type]
            RiskState.COLLECTING
        )


class TestPolicyLoader:
    def test_loads_committed_policy_bundle(self) -> None:
        from pathlib import Path

        from app.policy.loader import load_policy

        policy = load_policy(
            Path("policy/policy.yaml"),
            calibration_path=Path("policy/calibration.json"),
        )
        assert policy.version == "0.1.0"
        assert policy.thresholds.high_window_risk == 0.78
        assert policy.thresholds.evidence_k == 3
        assert policy.thresholds.evidence_n == 5

    def test_fails_closed_when_model_version_is_missing(self, tmp_path: Path) -> None:
        from pathlib import Path

        import yaml

        from app.policy.loader import PolicyLoadError, load_policy

        policy_bytes = Path("policy/policy.yaml").read_bytes()
        raw = yaml.safe_load(policy_bytes)
        del raw["model_version"]

        bad_policy = tmp_path / "bad_policy.yaml"
        bad_policy.write_text(yaml.dump(raw), encoding="utf-8")

        with pytest.raises(PolicyLoadError, match="policy bundle missing model_version"):
            load_policy(bad_policy, calibration_path=Path("policy/calibration.json"))

    def test_fails_closed_when_model_version_mismatches(self, tmp_path: Path) -> None:
        from pathlib import Path

        import yaml

        from app.policy.loader import PolicyLoadError, load_policy

        policy_bytes = Path("policy/policy.yaml").read_bytes()
        raw = yaml.safe_load(policy_bytes)
        raw["model_version"] = "wrong-model-version-999"

        bad_policy = tmp_path / "bad_policy.yaml"
        bad_policy.write_text(yaml.dump(raw), encoding="utf-8")

        with pytest.raises(PolicyLoadError, match="does not match the calibration artifact"):
            load_policy(bad_policy, calibration_path=Path("policy/calibration.json"))
