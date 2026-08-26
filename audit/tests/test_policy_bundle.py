"""The policy bundle and the calibration artifact.

These assert the two files that decide what the system *does*, and the gates that stop a placeholder
being presented as a measured result (rules.md R-01..R-04, R-11).

PyYAML is not installed in this environment, so the bundle is asserted twice over:

*Dependency-free.* A small indentation parser reads ``purpose_actions``, and the rest is line and token
inspection. These run everywhere, including a lane with nothing but the standard library. They are the
tests that will still be running in six months.

*Through the real loader,* behind ``importorskip("yaml")``. That is the only way to prove
``gateway/app/policy/loader.py`` actually accepts this file, so it is written and reported as skipped
rather than left out. ``app.policy.engine`` imports no third-party package, so the closed action
vocabulary and the ``evidence_k >= 2`` refusal are checked against the real code either way.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Final

import pytest
import schema_contract as sc
from app.policy.engine import Action, PolicyThresholds, RiskState
from tests.conftest import POLICY_DIR, REPO_ROOT
from tests.test_schema_allow_list import openapi_enum

POLICY_YAML: Final[Path] = POLICY_DIR / "policy.yaml"
CALIBRATION_JSON: Final[Path] = POLICY_DIR / "calibration.json"

#: Transcribed from ``gateway/app/policy/loader.py``. Not imported: that module needs PyYAML, and this
#: string is the gate — it has to be assertable in a lane that cannot import the loader at all.
PLACEHOLDER_STATUS: Final[str] = "placeholder-not-policy-eligible"

#: The literal that holds ``artifact_state`` at ``demo_eligible`` (loader.py::PolicyBundle).
PLACEHOLDER_DERIVATION: Final[str] = "placeholder"


# --------------------------------------------------------------------------------------------------
# Dependency-free readers
# --------------------------------------------------------------------------------------------------


def yaml_lines() -> list[str]:
    return POLICY_YAML.read_text(encoding="utf-8").splitlines()


def strip_comments(lines: list[str]) -> list[str]:
    """Drop comment lines and trailing comments.

    Necessary because ``policy.yaml`` names ``approve`` and ``deny`` in prose in order to forbid them.
    A token scan that did not strip comments would fail on the very text that documents the rule, and
    the fix somebody would reach for is deleting the explanation.
    """
    out: list[str] = []
    for line in lines:
        stripped = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
        out.append(stripped.rstrip())
    return out


def scalar(key: str) -> str:
    """The value of a top-level-or-nested ``key: value`` line, comments removed."""
    for line in strip_comments(yaml_lines()):
        match = re.match(rf"\s*{re.escape(key)}:\s*(.+)$", line)
        if match:
            return match.group(1).strip().strip('"').strip("'")
    raise AssertionError(f"{key} not found in policy.yaml")


def line_number_of(pattern: str) -> int:
    for index, line in enumerate(yaml_lines()):
        if re.search(pattern, line):
            return index
    raise AssertionError(f"{pattern!r} not found in policy.yaml")


def purpose_actions() -> dict[str, dict[str, str]]:
    """Parse the ``purpose_actions`` block by indentation.

    Hand-rolled rather than PyYAML so the proportionality assertions below run in every lane. The
    block is deliberately flat — purpose, then state, then a bare scalar — which is what makes 20 lines
    of parsing sufficient. If the block ever needs a real parser, it has become too clever for a file
    whose whole job is to be reviewable.
    """
    parsed: dict[str, dict[str, str]] = {}
    current: str | None = None
    inside = False
    for line in strip_comments(yaml_lines()):
        if not line.strip():
            continue
        if re.match(r"^purpose_actions:\s*$", line):
            inside = True
            continue
        if inside and not line.startswith(" "):
            break  # dedented to a new top-level key
        if not inside:
            continue
        indent = len(line) - len(line.lstrip())
        body = line.strip()
        if indent == 2 and body.endswith(":"):
            current = body[:-1]
            parsed[current] = {}
        elif indent == 4 and ":" in body:
            assert current is not None, f"orphan state line: {line!r}"
            state, action = (part.strip() for part in body.split(":", 1))
            parsed[current][state] = action
    return parsed


def calibration() -> dict[str, Any]:
    return json.loads(CALIBRATION_JSON.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------------------
# The threshold
# --------------------------------------------------------------------------------------------------


class TestThresholdIsMarkedAsAPlaceholder:
    """R-01..R-04. The number exists so the state machine has something to compare against. Every
    assertion here is about making that impossible to forget."""

    def test_the_threshold_is_0_78(self) -> None:
        assert float(scalar("high_window_risk")) == 0.78

    def test_the_derivation_is_the_literal_placeholder(self) -> None:
        """``loader.py::PolicyBundle.artifact_state`` compares this by string. Any other value asserts
        that a real derivation exists, so the exact literal is the gate."""
        assert scalar("derivation") == PLACEHOLDER_DERIVATION

    def test_the_placeholder_marking_sits_with_the_number_not_in_a_readme(self) -> None:
        """A caveat in a separate document is one the next person does not read. The declaration and
        the disclaimer have to be visible in the same screenful."""
        assert abs(line_number_of(r"^\s*high_window_risk:") - line_number_of(r"^\s*derivation: placeholder")) <= 10

    def test_the_file_itself_says_the_number_is_not_measured(self) -> None:
        text = POLICY_YAML.read_text(encoding="utf-8")
        assert "0.78 IS A PLACEHOLDER" in text
        assert "NOT A MEASURED VALUE" in text.upper()
        for rule in ("R-01", "R-02", "R-03", "R-04"):
            assert rule in text, f"the honesty rules are not cited next to the number: {rule}"

    def test_the_file_says_what_would_make_it_real(self) -> None:
        """"This is a placeholder" with no exit criteria is how a placeholder becomes permanent."""
        text = POLICY_YAML.read_text(encoding="utf-8")
        assert "cost" in text.lower() and "matrix" in text.lower()
        assert "dev_calibration" in text

    def test_no_word_in_the_bundle_claims_the_threshold_was_tuned(self) -> None:
        """The vocabulary that turns a placeholder into a claim. Searched over the whole file including
        comments, because a comment saying "tuned on our data" is exactly as quotable as a value."""
        text = POLICY_YAML.read_text(encoding="utf-8").lower()
        for claim in ("tuned to", "validated at", "optimal threshold", "measured at", "eer of"):
            assert claim not in text, f"the bundle claims the threshold was derived: {claim!r}"

    def test_the_threshold_is_inside_the_open_interval(self) -> None:
        """``PolicyThresholds`` rejects 0 and 1: a threshold of 0 makes every window high and 1 makes
        none, and both would present as a working demo with a broken detector."""
        PolicyThresholds(high_window_risk=float(scalar("high_window_risk")), evidence_k=3, evidence_n=5)
        with pytest.raises(ValueError, match=r"high_window_risk must be in \(0, 1\)"):
            PolicyThresholds(high_window_risk=1.0, evidence_k=3, evidence_n=5)


class TestEvidenceBar:
    """R-08. Three of the last five ELIGIBLE windows."""

    def test_the_bar_is_three_of_five(self) -> None:
        assert (int(scalar("k")), int(scalar("n"))) == (3, 5)

    def test_a_bundle_with_k_below_two_does_not_load(self) -> None:
        """Asserted against the real ``PolicyThresholds``, not a copy of its rule. "Lower k to 1 for a
        more responsive demo" is the specific change this refuses, and it refuses it at load time —
        before any audio arrives, rather than in a post-mortem."""
        with pytest.raises(ValueError, match="evidence_k must be >= 2"):
            PolicyThresholds(high_window_risk=0.78, evidence_k=1, evidence_n=5)

    def test_the_configured_bar_survives_the_real_validator(self) -> None:
        thresholds = PolicyThresholds(
            high_window_risk=float(scalar("high_window_risk")),
            evidence_k=int(scalar("k")),
            evidence_n=int(scalar("n")),
        )
        assert thresholds.evidence_k >= 2
        assert thresholds.evidence_n >= thresholds.evidence_k

    def test_the_bundle_explains_that_the_window_counts_eligible_windows_only(self) -> None:
        """R-09. The difference between "last five windows" and "last five eligible windows" is the
        difference between a system an attacker can dilute with a bad line and one they cannot."""
        text = POLICY_YAML.read_text(encoding="utf-8")
        assert "ELIGIBLE" in text
        assert "R-09" in text


# --------------------------------------------------------------------------------------------------
# The action map
# --------------------------------------------------------------------------------------------------


class TestActionVocabularyIsClosed:
    """R-07, at the third of the three definition sites: engine enum, database CHECK, and this file."""

    def test_every_configured_action_is_a_real_enum_member(self) -> None:
        configured = {action for states in purpose_actions().values() for action in states.values()}
        assert configured <= {a.value for a in Action}, configured - {a.value for a in Action}

    def test_the_enum_the_engine_exposes_matches_the_database_vocabulary(self) -> None:
        assert tuple(a.value for a in Action) == sc.ACTION_VOCABULARY

    @pytest.mark.privacy
    @pytest.mark.parametrize("banned", sc.FORBIDDEN_ACTION_VALUES)
    def test_no_authorization_verb_appears_as_a_configured_value(self, banned: str) -> None:
        """Comments are stripped first: the bundle names these words in prose to forbid them, and the
        explanation must not be the thing that fails the test."""
        for number, line in enumerate(strip_comments(yaml_lines()), start=1):
            assert not re.search(rf"[:\-]\s*{banned}\s*$", line), f"policy.yaml:{number}: {line!r}"

    def test_the_bundle_states_that_the_vocabulary_is_closed(self) -> None:
        text = POLICY_YAML.read_text(encoding="utf-8")
        assert "R-07" in text
        assert "closed" in text.lower()


class TestPurposeActionMap:
    def test_the_purposes_are_exactly_the_contract_enum(self) -> None:
        """A purpose here that the API rejects is dead configuration; a purpose the API accepts that is
        missing here is a ``ValueError`` when ``PolicyEngine`` is constructed — i.e. a failed session
        for one specific transaction type, discovered by whoever tries it first."""
        assert set(purpose_actions()) == set(openapi_enum("PurposeCode"))

    def test_every_purpose_maps_all_three_risk_states(self) -> None:
        """An unmapped state is a ``KeyError`` at decision time: a crash during the demo at the exact
        moment risk was detected. ``loader.py::_parse_purpose_actions`` refuses the bundle instead."""
        states = {s.value for s in RiskState}
        for purpose, mapping in purpose_actions().items():
            assert set(mapping) == states, f"{purpose}: {sorted(set(mapping) ^ states)}"

    def test_collecting_never_triggers_an_action(self) -> None:
        """R-08 as a property of the configuration rather than of the engine.

        ``collecting`` means the k-of-n window is not yet full — there is no evidence bar met. Anything
        other than ``continue`` here would act on less evidence than the bar requires, which is the
        same defect as setting ``k: 1`` and is invisible in the threshold.
        """
        for purpose, mapping in purpose_actions().items():
            assert mapping["collecting"] == "continue", f"{purpose} acts before the evidence bar"

    def test_high_is_never_ignored(self) -> None:
        """The other end of the same argument. If any purpose mapped ``high`` to ``continue``, the
        evidence bar could be met and nothing would happen — a detector wired to nothing."""
        for purpose, mapping in purpose_actions().items():
            assert mapping["high"] != "continue", f"{purpose} ignores a met evidence bar"

    def test_the_same_evidence_produces_different_actions_for_different_purposes(self) -> None:
        """R-05/R-06, and the reason this is a map rather than a global threshold. Identical evidence
        must be able to produce a different response depending on what is at stake, or the system
        either over-reacts to a balance query or under-reacts to an account takeover."""
        high_actions = {p: m["high"] for p, m in purpose_actions().items()}
        assert len(set(high_actions.values())) >= 2, high_actions

    def test_the_named_pairing_from_the_brief_holds(self) -> None:
        """Same risk state, same evidence, two different actions — because money leaving an account can
        be resolved by a step-up, and an account takeover cannot."""
        mapping = purpose_actions()
        assert mapping["payment_release"]["high"] == "hold"
        assert mapping["account_recovery"]["high"] == "escalate"

    def test_a_zero_stakes_purpose_is_treated_proportionately(self) -> None:
        """R-06. Interrupting a balance query with identity checks is a false-positive cost paid by the
        customer, and the bundle has to be able to express "nothing is at risk here"."""
        mapping = purpose_actions()
        assert mapping["support_enquiry"]["uncertain"] == "continue"
        assert mapping["support_enquiry"]["high"] == "verify"


class TestStickyHighIsDocumentedNotConfigurable:
    """R-13. The property is enforced in the engine; the bundle records where, and nothing more."""

    def test_the_invariants_block_holds_code_locations_not_booleans(self) -> None:
        """A ``true`` in a config file implies a ``false`` exists, and somebody eventually sets it. The
        values are file-and-symbol references so there is no switch to flip."""
        block = re.search(
            r"^enforced_invariants:\n(.*?)(?=^\w)", POLICY_YAML.read_text(encoding="utf-8"), re.M | re.S
        )
        assert block, "policy.yaml has no enforced_invariants block"
        entries = re.findall(r"^\s{2}(\w+):\s*\"([^\"]+)\"", block.group(1), re.M)
        assert entries, "no invariant entries parsed"
        for name, value in entries:
            assert value not in ("true", "false"), f"{name} looks like a switch"
            assert "::" in value, f"{name} does not name a code location: {value!r}"

    def test_every_named_code_location_exists(self) -> None:
        """The failure this catches is a refactor that moves the enforcement and leaves the bundle
        pointing at a symbol that no longer exists — a documented guarantee with nothing behind it."""
        block = POLICY_YAML.read_text(encoding="utf-8")
        entries = re.findall(r"\"([\w/.]+\.py)::([\w.]+)[^\"]*\"", block)
        assert entries, "no code locations parsed from enforced_invariants"
        for relative_path, symbol in entries:
            path = REPO_ROOT / relative_path
            assert path.is_file(), f"{relative_path} does not exist"
            leaf = symbol.split(".")[-1]
            assert re.search(rf"\b(class|def)\s+{leaf}\b", path.read_text(encoding="utf-8")), (
                f"{relative_path} does not define {leaf}"
            )

    def test_sticky_high_is_cited_with_its_rule_id(self) -> None:
        text = POLICY_YAML.read_text(encoding="utf-8")
        assert "R-13" in text
        assert "sticky" in text.lower()


class TestBundleIdentityAndHashDiscipline:
    def test_the_bundle_declares_a_version(self) -> None:
        assert re.match(sc.VERSION_REGEX, scalar("policy_version")), scalar("policy_version")

    def test_the_version_fits_the_audit_column_constraint(self) -> None:
        """It is written into every audit row as ``policy_version`` and the CHECK bounds its shape. A
        long or exotic version string would fail the first audit write, not the config review."""
        for key in ("policy_version", "model_version"):
            assert re.match(sc.VERSION_REGEX, scalar(key)), key

    def test_the_bundle_does_not_claim_to_know_its_own_hash(self) -> None:
        """It structurally cannot: the digest is over the bytes, so any digest inside the file is stale
        the moment it is written. A stale digest is worse than none — it invites a comparison that
        always fails, and the fix people reach for is disabling the comparison."""
        for line in strip_comments(yaml_lines()):
            assert not re.match(r"\s*\w*sha256\w*:", line), f"self-referential digest field: {line!r}"

    def test_the_file_explains_that_its_bytes_are_the_canonical_form(self) -> None:
        """The loader hashes raw bytes, so a reformat is a policy change in the audit trail. Somebody
        has to be told before they run a YAML formatter over it."""
        text = POLICY_YAML.read_text(encoding="utf-8")
        assert "CANONICAL FORM" in text
        assert "policy_bundle_sha256" in text
        assert "formatter" in text.lower()

    def test_the_model_version_matches_the_calibration_artifact(self) -> None:
        """``loader.py`` refuses the pair otherwise. A bundle tuned for one model deployed against
        another applies every threshold to a different score distribution, and nothing in the output
        would look wrong."""
        assert scalar("model_version") == calibration()["model_version"]

    def test_the_bundle_is_a_single_mapping_with_a_reviewed_key_set(self) -> None:
        """Top-level keys are enumerated so a new one is a deliberate act. ``loader.py`` ignores keys it
        does not know, so an unnoticed addition is config that looks live and is not."""
        keys = {
            line.split(":", 1)[0]
            for line in strip_comments(yaml_lines())
            if line and not line.startswith((" ", "-"))
        }
        assert keys == {
            "policy_version",
            "model_version",
            "thresholds",
            "evidence",
            "purpose_actions",
            "enforced_invariants",
            "provenance",
        }, sorted(keys)


# --------------------------------------------------------------------------------------------------
# Calibration artifact
# --------------------------------------------------------------------------------------------------


class TestCalibrationIsAPlaceholder:
    """R-11/R-37. It must load, and it must be impossible to mistake for a fitted calibration."""

    def test_it_is_valid_json_with_the_keys_the_loader_requires(self) -> None:
        data = calibration()
        for key in ("calibration_version", "status", "model_version", "model_sha256"):
            assert key in data, key

    def test_the_status_is_the_exact_gating_literal(self) -> None:
        """``loader.py::CalibrationArtifact.is_policy_eligible`` is a string comparison against this
        value. It is the only thing holding ``policy_eligible`` shut."""
        assert calibration()["status"] == PLACEHOLDER_STATUS

    def test_the_warning_is_the_first_thing_in_the_file(self) -> None:
        """JSON has no comments, so the disclaimer has to be a key. Making it the first key means it is
        the first thing a reader and every diff sees."""
        text = CALIBRATION_JSON.read_text(encoding="utf-8")
        keys = re.findall(r'^\s{2}"(\w+)"', text, re.M)
        assert keys[0] == "WARNING", keys[:3]
        assert "NOT A FITTED CALIBRATION" in calibration()["WARNING"]

    def test_it_declares_itself_unfitted(self) -> None:
        assert calibration()["fitted"] is False

    def test_the_platt_parameters_are_the_identity_transform(self) -> None:
        """The structural safeguard, and the reason this artifact cannot be mistaken for a fitted one.

        Plausible-looking coefficients would silently alter every score, and no consumer could tell a
        placeholder mapping from a fitted one by looking at the output. With the identity,
        ``raw_score == spoof_risk`` and the two are distinguishable by inspection.
        """
        platt = calibration()["platt"]
        assert (platt["a"], platt["b"]) == (1.0, 0.0)
        assert platt["is_identity"] is True

    def test_the_identity_claim_is_arithmetically_true(self) -> None:
        """Asserts the transform, not the flag. If someone edits ``a`` and leaves ``is_identity: true``,
        the flag becomes a lie and this is what catches it."""
        platt = calibration()["platt"]
        a, b = float(platt["a"]), float(platt["b"])
        for raw in (0.01, 0.1, 0.42, 0.78, 0.99):
            logit = math.log(raw / (1.0 - raw))
            calibrated = 1.0 / (1.0 + math.exp(-(a * logit + b)))
            assert math.isclose(calibrated, raw, rel_tol=1e-12), (raw, calibrated)

    def test_no_reliability_metric_carries_a_fabricated_number(self) -> None:
        """``null``, not ``0.0``. A zero ECE reads as perfectly calibrated — the single most misleading
        value this file could hold — and an omitted key lets a consumer treat "not measured" as
        "passing"."""
        reliability = calibration()["reliability"]
        measured = {k: v for k, v in reliability.items() if not k.endswith("note")}
        assert measured, "the reliability block has no metric fields at all"
        assert all(value is None for value in measured.values()), measured

    def test_the_sample_count_is_zero_rather_than_absent(self) -> None:
        assert calibration()["fit"]["n_samples"] == 0
        assert calibration()["fit"]["fitted_at"] is None

    def test_the_model_digest_is_empty_rather_than_a_plausible_hash(self) -> None:
        """Empty is legible as "no artifact paired". A made-up 64-hex digest would pass every shape
        check in the system and match nothing, and the mismatch would surface as a Scorer startup
        failure with no explanation."""
        assert calibration()["model_sha256"] == ""
        assert re.match(sc.HEX64_OR_EMPTY_REGEX, calibration()["model_sha256"])

    def test_the_version_is_marked_as_a_placeholder_in_its_own_string(self) -> None:
        """It is stamped into every audit row as ``calibration_version``. Anyone reading the evidence
        table months later must be able to tell, from that column alone, that no calibration was
        applied."""
        version = calibration()["calibration_version"]
        assert "placeholder" in version.lower()
        assert re.match(sc.VERSION_REGEX, version), version


class TestCalibrationSplitDiscipline:
    """R-37. The rule that cannot be un-broken once broken."""

    def test_the_permitted_fit_split_is_dev_calibration(self) -> None:
        assert calibration()["fit"]["permitted_fit_split"] == "dev_calibration"

    def test_eval_locked_is_named_as_forbidden(self) -> None:
        assert calibration()["fit"]["forbidden_fit_split"] == "eval_locked"

    def test_nothing_has_been_fitted_on_any_split_yet(self) -> None:
        assert calibration()["fit"]["fitted_on_split"] is None

    def test_the_fitted_split_may_never_become_eval_locked(self) -> None:
        """The assertion a future commit will trip. Fitting on ``eval_locked`` turns a held-out estimate
        into a training estimate, and re-running the split does not restore its independence."""
        assert calibration()["fit"]["fitted_on_split"] != "eval_locked"

    def test_the_artifact_explains_why_and_cites_the_rule(self) -> None:
        text = CALIBRATION_JSON.read_text(encoding="utf-8")
        assert "R-37" in text
        assert "R-38" in text, "the grouping-before-augmentation requirement is not mentioned"

    def test_promotion_requires_more_than_editing_the_status(self) -> None:
        """A checklist in the file, in order, so "make it policy_eligible" cannot be a one-line diff
        that nobody notices in review."""
        checklist = calibration()["promotion_checklist"]
        assert len(checklist) >= 6
        joined = " ".join(checklist)
        assert "dev_calibration" in joined
        assert "manifest_sha256" in joined
        assert PLACEHOLDER_STATUS in joined

    def test_the_artifact_cannot_promote_itself(self) -> None:
        """``artifact_state`` is derived by the loader from ``status`` and the bundle's ``derivation``.
        There is deliberately no ``policy_eligible: true`` to set here."""
        data = calibration()
        assert data["eligibility"]["artifact_state_ceiling"] == "demo_eligible"
        assert data["eligibility"]["probability_language_permitted"] is False
        assert "policy_eligible" not in {
            k for k, v in data["eligibility"].items() if v is True
        }


# --------------------------------------------------------------------------------------------------
# Through the real loader
# --------------------------------------------------------------------------------------------------


class TestTheRealLoaderAcceptsTheBundle:
    """UNVERIFIED in this environment: PyYAML is not installed and there is no network to install it.
    Every test here skips. They exist because the dependency-free tests above cannot prove that
    ``loader.py`` parses this file — only that it says what the loader is expected to want."""

    def loader(self):
        pytest.importorskip("yaml", reason="PyYAML is unavailable; the loader round-trip is UNVERIFIED")
        from app.policy import loader as module

        return module

    def test_the_bundle_loads(self) -> None:
        module = self.loader()
        bundle = module.load_policy(POLICY_YAML, CALIBRATION_JSON)
        assert bundle.version == scalar("policy_version")
        assert bundle.thresholds.evidence_k == 3
        assert bundle.thresholds.evidence_n == 5
        assert len(bundle.sha256) == 64

    def test_the_derived_state_is_demo_eligible_and_cannot_be_higher(self) -> None:
        module = self.loader()
        bundle = module.load_policy(POLICY_YAML, CALIBRATION_JSON)
        assert bundle.artifact_state == "demo_eligible"
        assert bundle.allows_probability_language is False
        assert bundle.threshold_derivation == PLACEHOLDER_DERIVATION

    def test_the_placeholder_status_constant_matches_this_files_transcription(self) -> None:
        module = self.loader()
        assert module.PLACEHOLDER_STATUS == PLACEHOLDER_STATUS

    def test_an_authorization_action_is_refused(self, tmp_path: Path) -> None:
        module = self.loader()
        mutated = tmp_path / "policy.yaml"
        mutated.write_text(
            POLICY_YAML.read_text(encoding="utf-8").replace("high: hold", "high: deny", 1),
            encoding="utf-8",
        )
        with pytest.raises(module.PolicyLoadError, match="not a valid action"):
            module.load_policy(mutated, CALIBRATION_JSON)

    def test_a_lowered_evidence_bar_is_refused(self, tmp_path: Path) -> None:
        module = self.loader()
        mutated = tmp_path / "policy.yaml"
        mutated.write_text(
            POLICY_YAML.read_text(encoding="utf-8").replace("k: 3", "k: 1", 1), encoding="utf-8"
        )
        with pytest.raises(module.PolicyLoadError, match="evidence_k must be >= 2"):
            module.load_policy(mutated, CALIBRATION_JSON)

    def test_a_mismatched_model_version_is_refused(self, tmp_path: Path) -> None:
        module = self.loader()
        mutated = tmp_path / "calibration.json"
        data = calibration()
        data["model_version"] = "9.9.9-other-model"
        mutated.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(module.PolicyLoadError, match="model_version does not match"):
            module.load_policy(POLICY_YAML, mutated)

    def test_every_purpose_the_contract_allows_can_build_an_engine(self) -> None:
        """The failure this catches: a session type the API accepts and the engine cannot serve, which
        surfaces as a crash for one specific transaction type."""
        module = self.loader()
        from app.policy.engine import PolicyEngine

        bundle = module.load_policy(POLICY_YAML, CALIBRATION_JSON)
        for purpose in openapi_enum("PurposeCode"):
            engine = PolicyEngine(bundle.thresholds, purpose, bundle.purpose_actions)
            assert engine.action_for(RiskState.COLLECTING) is Action.CONTINUE
