"""The detection/decision seam is STRUCTURAL. This suite inspects the generated descriptor.

rules.md R-10 and technical-design.md §3: the Scorer produces a number, the Gateway decides. That is not
a convention this service follows — it is a property of the message shapes in
``contracts/voice_scorer.proto``. The Scorer receives no ``purpose_code``, no session history, and no
action field, and it has nowhere to return one. There is no code path in ``scorer/app`` that could take a
policy input because the wire format has no slot for it.

WHY THE TEST READS THE DESCRIPTOR RATHER THAN THE .proto TEXT
The generated descriptor is what the running code actually sees. A field could be added to the ``.proto``
and the stubs regenerated, or a field could be added by any other route into the generated module; either
way the descriptor is the ground truth for both the Gateway and the Scorer. Grepping the ``.proto`` source
would also pass on a stale generated pair, which is the case where the two services genuinely disagree.

If any assertion here fails, the correct response is NOT to update the expected set. It is a contract
change under the two-key review rule in ``contracts/CONTRACT_CHANGE_POLICY.md``, and a policy-shaped
field on this service is a design violation regardless of who approved the proto edit.
"""

from __future__ import annotations

import pytest

from app import voice_scorer_pb2 as pb

pytestmark = pytest.mark.contract


#: The exact field set. Asserted by equality, not by containment: an unexpected field fails even if it
#: passes the heuristic scan below, which forces a human to look at every addition to this contract.
EXPECTED_REQUEST_FIELDS = frozenset(
    {"pcm_window", "contract_id", "sample_rate_hz", "window_seq", "session_ref"}
)
EXPECTED_RESPONSE_FIELDS = frozenset(
    {
        "spoof_risk",
        "model_version",
        "calibration_version",
        "quality_flags",
        "eligible",
        "raw_score",
        "scorer_latency_us",
        "detector_mode",
    }
)
EXPECTED_HEALTH_FIELDS = frozenset(
    {
        "ready",
        "execution_provider",
        "model_version",
        "model_sha256",
        "calibration_version",
        "calibration_sha256",
        "detector_mode",
        "artifact_state",
        "contract_vector_parity_ok",
    }
)

#: Name TOKENS (underscore-separated) that mark a field as policy-shaped. Deliberately token-based
#: rather than substring-based: ``spoof_risk`` contains "risk" and ``session_ref`` contains "session",
#: and both are legitimate. A substring scan would either false-positive on those or be watered down
#: until it caught nothing.
FORBIDDEN_TOKENS = frozenset(
    {
        "purpose",
        "action",
        "decision",
        "decide",
        "allow",
        "deny",
        "block",
        "hold",
        "threshold",
        "policy",
        "verdict",
        "disposition",
        "escalate",
        "escalation",
        "challenge",
        "recommend",
        "recommendation",
        "reason",
        "consecutive",
        "history",
        "prior",
        "previous",
        "ticket",
        "consent",
        "transcript",
    }
)

#: Full names that are policy-shaped without containing a forbidden token on their own.
FORBIDDEN_NAMES = frozenset(
    {"risk_state", "k_of_n", "step_up", "session_history", "window_history", "evidence_count"}
)


def _field_names(message: type) -> frozenset[str]:
    return frozenset(message.DESCRIPTOR.fields_by_name.keys())


def _policy_shaped(names: frozenset[str]) -> list[str]:
    offenders = []
    for name in sorted(names):
        if name in FORBIDDEN_NAMES or FORBIDDEN_TOKENS & set(name.split("_")):
            offenders.append(name)
    return offenders


class TestScoreWindowRequestCarriesNoPolicyInput:
    """What the Scorer is given. Nothing here can express intent, purpose, or accumulated evidence."""

    def test_request_field_set_is_exactly_as_designed(self) -> None:
        """Prevents a policy input arriving through a field nobody reviewed.

        Equality, not containment. A new field on this message is a contract change under
        ``contracts/CONTRACT_CHANGE_POLICY.md``; failing here is the intended way for that to surface.
        """
        assert _field_names(pb.ScoreWindowRequest) == EXPECTED_REQUEST_FIELDS

    def test_no_request_field_is_policy_shaped(self) -> None:
        """Prevents ``purpose_code``, ``threshold``, or a session history reaching the detector.

        A detector that can see the purpose of a call can be tuned per purpose, and a detector that can
        see prior windows can accumulate its own evidence. Either one dissolves the seam that makes the
        decision auditable: the audit row would no longer be able to say which component decided what.
        """
        offenders = _policy_shaped(_field_names(pb.ScoreWindowRequest))
        assert not offenders, f"policy-shaped field(s) on ScoreWindowRequest: {offenders}"

    def test_request_carries_no_nested_message(self) -> None:
        """Prevents a policy object smuggled in as a sub-message with an innocuous field name.

        ``context``, ``metadata``, or ``options`` would each pass a name scan while carrying an arbitrary
        payload. Every field on this message is a scalar or bytes, so there is no container for one.
        """
        for field in pb.ScoreWindowRequest.DESCRIPTOR.fields:
            assert field.message_type is None, f"{field.name} is a nested message"

    def test_request_has_no_map_or_repeated_field(self) -> None:
        """Prevents an open-ended key/value bag becoming the policy channel.

        A ``map<string, string> attributes`` field satisfies every other test in this file and is a
        general-purpose side channel into the detector.
        """
        for field in pb.ScoreWindowRequest.DESCRIPTOR.fields:
            assert field.label != field.LABEL_REPEATED, f"{field.name} is repeated"

    def test_window_seq_exists_but_is_not_evidence(self) -> None:
        """The one field that could be misused is present for ordering only.

        ``window_seq`` is on the message because the Gateway needs to correlate its own request with its
        own response. The proto states the Scorer must not use it to correlate windows, and
        ``test_model.py::TestMockDeterminism`` asserts the mock's score does not depend on it — so the
        prohibition is checked, not merely written down.
        """
        assert "window_seq" in _field_names(pb.ScoreWindowRequest)


class TestScoreWindowResponseCarriesNoDecision:
    """What the Scorer returns. A calibrated number plus provenance — never an action."""

    def test_response_field_set_is_exactly_as_designed(self) -> None:
        assert _field_names(pb.ScoreWindowResponse) == EXPECTED_RESPONSE_FIELDS

    def test_no_response_field_is_policy_shaped(self) -> None:
        """Prevents the Scorer returning an action, a verdict, or a risk state.

        The moment this message can carry ``action = HOLD``, the Gateway's policy engine becomes
        advisory and the audit trail can no longer attribute the decision. Every rule in the policy
        bundle would then have a second, undocumented implementation inside a model artifact.
        """
        offenders = _policy_shaped(_field_names(pb.ScoreWindowResponse))
        assert not offenders, f"policy-shaped field(s) on ScoreWindowResponse: {offenders}"

    def test_spoof_risk_and_raw_score_are_both_present(self) -> None:
        """Both, deliberately: the calibrated value for policy, the raw value for the parity gate.

        Collapsing them into one field would make the ONNX parity gate (frame_contract.md §6)
        unmeasurable through the RPC, because a calibration change would move the only number available.
        """
        fields = pb.ScoreWindowResponse.DESCRIPTOR.fields_by_name
        assert fields["spoof_risk"].type == fields["spoof_risk"].TYPE_FLOAT
        assert fields["raw_score"].type == fields["raw_score"].TYPE_FLOAT

    def test_eligible_is_a_bool_not_a_disqualification_reason(self) -> None:
        """Prevents the quality assessment growing into a second decision surface.

        ``eligible`` says whether the window counts, and the flags say what was observed. Neither says
        what to do about it (rules.md R-09).
        """
        fields = pb.ScoreWindowResponse.DESCRIPTOR.fields_by_name
        assert fields["eligible"].type == fields["eligible"].TYPE_BOOL


class TestServiceSurface:
    """Two RPCs. Nothing that could carry a session, a decision, or a stream of state."""

    def test_service_has_exactly_two_methods(self) -> None:
        """Prevents a stateful streaming RPC appearing, which would give the Scorer session memory."""
        service = pb.DESCRIPTOR.services_by_name["VoiceScorer"]
        assert {method.name for method in service.methods} == {"ScoreWindow", "Health"}

    def test_neither_rpc_is_streaming(self) -> None:
        """Prevents a bidirectional stream, in which the Scorer would hold per-connection state.

        One window in, one score out, no memory between calls. That is what makes "the Scorer is
        stateless" a checkable claim rather than an intention.
        """
        service = pb.DESCRIPTOR.services_by_name["VoiceScorer"]
        for method in service.methods:
            assert not method.client_streaming
            assert not method.server_streaming

    def test_health_field_set_is_exactly_the_parity_set(self) -> None:
        """Prevents a parity-set field being dropped, which would make the Gateway's check vacuous.

        ``gateway/app/main.py`` refuses to start when the Scorer's ``execution_provider`` differs from its
        own, and when ``model_sha256`` disagrees with the policy bundle's. Both checks read fields on this
        message; removing one would turn a hard refusal into a silently skipped comparison.
        """
        assert _field_names(pb.HealthResponse) == EXPECTED_HEALTH_FIELDS

    def test_health_request_is_empty(self) -> None:
        """Prevents health being parameterised, which is how it becomes a debug/introspection endpoint."""
        assert _field_names(pb.HealthRequest) == frozenset()


class TestDetectorModeEnum:
    """rules.md R-46: the mock label is part of the contract, not a runtime convention."""

    def test_mock_mode_is_spelled_unmistakably(self) -> None:
        """Prevents the label being shortened to something a reader's eye skips.

        ``MOCK`` in a log line or a metric label is skimmable. ``MOCK_SMOKE_MODE_NOT_A_DETECTOR`` is not,
        and it is the label attached to every score, every health response, and every log line this
        service emits.
        """
        assert pb.DetectorMode.Name(2) == "MOCK_SMOKE_MODE_NOT_A_DETECTOR"

    def test_unspecified_is_zero_so_an_unset_field_is_never_real(self) -> None:
        """Prevents a default-initialised message reading as REAL_DETECTOR.

        proto3 scalar defaults are zero. If ``REAL_DETECTOR`` were 0, a response that failed to set the
        field — or a message decoded from an older writer — would claim a real detector by omission.
        """
        assert pb.DetectorMode.Name(0) == "DETECTOR_MODE_UNSPECIFIED"
        assert pb.DetectorMode.Value("REAL_DETECTOR") != 0

    def test_quality_flag_unspecified_is_zero(self) -> None:
        """Same reasoning: an unset or unknown flag must not decode as a specific observation."""
        assert pb.QualityFlag.Name(0) == "QUALITY_FLAG_UNSPECIFIED"
