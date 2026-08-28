"""Tests for the eight evaluation gate templates in ``evaluation/reports/``.

WHAT THESE TESTS DEFEND. A template's only job is to be blank and to ask the right questions. Both
halves rot in predictable ways:

  * **A number creeps in.** Somebody adds an illustrative figure — a "typical" EER, an example p95,
    a sample tolerance. The template gets copied, three cells get filled, and the illustrative
    values are read as measurements by everyone downstream. This is the same failure the placeholder
    marking on ``policy/policy.yaml``'s threshold prevents (rules.md R-01..R-04), and it is worse in
    a report, because a report is *for* reporting numbers and nothing about the file signals which
    ones were measured.
  * **A gate loses its teeth.** The blocker semantics live in prose, so a well-meaning edit can turn
    "block the release" into "review before release". The two blockers are asserted by name here so
    that softening one fails the suite.

So: every value cell must be the literal ``___``, no result-shaped literal may appear anywhere, and
the deploy/release blocker language is pinned.

WHY ``___`` AND NOT ``TBD``. A single unambiguous token can be machine-checked and grepped. "TBD",
"N/A", and an empty cell are all indistinguishable from a cell somebody forgot, and an empty cell in
particular renders as a filled-looking table.

Playbook reference: PS104_AI_Training_and_Evaluation_Playbook.md 6.1 (the gate table, quoted
verbatim in each template).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTS_DIR = REPO_ROOT / "evaluation" / "reports"

BLANK = "`___`"

# The eight gates of playbook 6.1, in order, with the filename each must live in and the exact
# pass condition the template must quote. Transcribed from the playbook rather than read out of the
# templates: a test that reads the answer from the file under test asserts only that the file equals
# itself.
GATES: tuple[tuple[str, str, str], ...] = (
    (
        "gate-1-data.md",
        "Data lead",
        "Manifest validated; no split leakage; licence/consent present",
    ),
    (
        "gate-2-baseline.md",
        "ML lead",
        "AASIST exceeds or matches LFCC-LCNN and RawNet2 on declared dev protocol",
    ),
    (
        "gate-3-ood.md",
        "ML + evaluation lead",
        "Report generator-, codec-, language-, and device-held-out results",
    ),
    (
        "gate-4-calibration.md",
        "ML lead",
        "Improved ECE/Brier on dev without harmful locked-set regression",
    ),
    (
        "gate-5-onnx-parity.md",
        "ML + platform lead",
        "Output ranking and calibrated decisions match reference within predeclared tolerance",
    ),
    (
        "gate-6-quantization.md",
        "ML lead",
        "Locked-set metrics, calibration, and temporal policy remain acceptable",
    ),
    (
        "gate-7-privacy.md",
        "Privacy lead",
        "No raw audio/transcript/embedding in audit/log export",
    ),
    (
        "gate-8-demo.md",
        "Team lead",
        "AWS GPU and local CPU run same test trace with recorded latency",
    ),
)

FAILURE_RESPONSES: dict[str, str] = {
    "gate-1-data.md": "Stop training and repair provenance/splits",
    "gate-2-baseline.md": "Investigate input pipeline before adding features",
    "gate-3-ood.md": "Restrict claim or retain `uncertain` policy; do not hide gap",
    "gate-4-calibration.md": "Freeze simpler calibrator or retrain",
    "gate-5-onnx-parity.md": "Block deployment artifact",
    "gate-6-quantization.md": "Retain FP32 model",
    "gate-7-privacy.md": "Block demo release",
    "gate-8-demo.md": "Fix parity or present one tier only, truthfully",
}

GATE_FILES = tuple(name for name, _, _ in GATES)

# Header labels whose column is a place for a measurement. A cell under one of these must be blank.
VALUE_COLUMNS = frozenset(
    {
        "value",
        "measured",
        "result",
        "results",
        "gpu tier",
        "cpu tier",
        "cpu fallback tier",
        "fp32",
        "quantized",
        "reference",
        "onnx",
        "candidate",
        "uncalibrated",
        "simpler alternative",
        "previous artifact",
        "samples",
        "count",
        "eer",
        "eer mean",
        "eer range",
        "roc-auc",
        "roc-auc mean",
        "roc-auc range",
        "pr-auc",
        "pr-auc mean",
        "min t-dcf",
        "fnr",
        "seeds",
        "records",
        "bona fide",
        "spoof",
        "distinct speakers",
        "distinct generators",
        "languages",
        "disagreements",
        "improvement",
        "findings",
        "budget met",
        "rank inversions",
    }
)


def read(name: str) -> str:
    return (REPORTS_DIR / name).read_text(encoding="utf-8")


def prose(name: str) -> str:
    """Whole file with runs of whitespace collapsed to single spaces.

    Prose assertions below match against this rather than the raw text. A sentence that spans a
    line break is the same sentence, and a test that fails when somebody re-wraps a paragraph
    teaches people to stop running the suite.
    """
    return re.sub(r"\s+", " ", read(name))


def table_rows(text: str) -> list[tuple[list[str], list[str], int]]:
    """Yield ``(headers, cells, line_number)`` for every markdown table body row.

    A hand-rolled parser rather than a markdown library, because there is no markdown library in
    this environment and the grammar needed here is two lines deep.
    """
    rows: list[tuple[list[str], list[str], int]] = []
    headers: list[str] = []
    lines = text.splitlines()
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped.startswith("|"):
            headers = []
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if set("".join(cells)) <= set("-: "):  # the |---|---| separator
            continue
        if not headers:
            headers = [c.lower() for c in cells]
            continue
        rows.append((headers, cells, i))
    return rows


# ==================================================================================================
class TestAllEightGatesExist:
    def test_every_gate_has_a_template(self) -> None:
        for name, _, _ in GATES:
            assert (REPORTS_DIR / name).is_file(), f"{name} is missing"

    def test_there_are_exactly_eight_plus_the_index(self) -> None:
        present = sorted(p.name for p in REPORTS_DIR.glob("*.md"))
        assert present == sorted([*GATE_FILES, "README.md"]), (
            "an extra file here is most likely a FILLED report committed over a template; filled "
            "reports belong in a dated copy, not next to the blanks"
        )

    def test_the_readme_indexes_every_gate(self) -> None:
        readme = read("README.md")
        for name, _, _ in GATES:
            assert name in readme, f"{name} is not linked from the index"

    def test_the_numbering_is_the_playbook_order(self) -> None:
        # The gates are ordered by dependency: data before baseline before OOD before calibration
        # before export. A renumbering would silently reorder the dependency chain in §"Requires".
        for i, (name, _, _) in enumerate(GATES, start=1):
            assert name.startswith(f"gate-{i}-")


# ==================================================================================================
@pytest.mark.parametrize("name", GATE_FILES)
class TestNoPrefilledNumbers:
    """The brief's hardest constraint on this directory: a template has no pre-filled numbers."""

    def test_every_value_cell_is_blank(self, name: str) -> None:
        offenders: list[str] = []
        for headers, cells, line in table_rows(read(name)):
            for header, cell in zip(headers, cells):
                if header not in VALUE_COLUMNS:
                    continue
                if cell in (BLANK, "—", "-", "", "zero"):
                    continue
                offenders.append(f"{name}:{line} column {header!r} holds {cell!r}")
        assert offenders == [], (
            "value cells must be blank. A number in a template survives being copied: three cells "
            "get filled, the rest are read as measurements.\n" + "\n".join(offenders)
        )

    def test_no_percentage_appears(self, name: str) -> None:
        hits = re.findall(r"\d+\s*(?:%|percent)", read(name))
        assert hits == [], f"{name} contains a percentage figure: {hits}"

    def test_no_latency_figure_appears(self, name: str) -> None:
        hits = re.findall(r"\d+\s*(?:ms|milliseconds|seconds|sec\b)", read(name))
        assert hits == [], f"{name} contains a latency figure: {hits}"

    def test_no_multi_decimal_constant_appears(self, name: str) -> None:
        # Two or more digits after the point is a measurement or an operating point (0.78, 2.56,
        # 0.0031). One digit is almost always a section reference (playbook 6.1), which is why the
        # rule is written on the number of decimal places rather than on the presence of a dot.
        hits = re.findall(r"\b\d+\.\d{2,}\b", read(name))
        assert hits == [], (
            f"{name} contains {hits}, which reads as a measurement or an operating point. If a "
            "spec constant is meant, cite the document that declares it instead of restating it."
        )

    def test_no_threshold_value_is_restated(self, name: str) -> None:
        # 0.78 specifically: policy/policy.yaml marks it a placeholder in the file that holds it,
        # and a report that repeats the digits loses that marking.
        assert "0.78" not in read(name), (
            f"{name} restates the placeholder threshold. Refer to policy/policy.yaml's "
            "high_window_risk by name; the marking lives with the number."
        )

    def test_no_metric_name_is_followed_by_a_figure(self, name: str) -> None:
        pattern = r"(?:EER|ECE|Brier|AUC|TPR|FPR|FNR|p50|p95|p99)\s*(?:of|=|:)?\s*\d"
        hits = re.findall(pattern, read(name))
        assert hits == [], f"{name} pairs a metric name with a figure: {hits}"

    def test_the_status_is_not_run(self, name: str) -> None:
        assert "`not-run`" in read(name), (
            "a template whose status reads anything else is a report claiming to have been run"
        )

    def test_the_title_says_template(self, name: str) -> None:
        first = read(name).splitlines()[0]
        assert "TEMPLATE" in first and "NOT YET RUN" in first, (
            f"{name} first line is {first!r}; a copied template that loses this marker is "
            "indistinguishable from a real report"
        )

    def test_no_verdict_is_pre_checked(self, name: str) -> None:
        checked = re.findall(r"- \[[xX]\]", read(name))
        assert checked == [], f"{name} has a pre-ticked verdict box: {checked}"

    def test_a_verdict_box_exists_for_pass_fail_and_not_run(self, name: str) -> None:
        text = read(name)
        assert re.search(r"- \[ \] \*\*PASS", text), f"{name} has no PASS box"
        assert re.search(r"- \[ \] \*\*FAIL", text), f"{name} has no FAIL box"
        assert re.search(r"- \[ \] \*\*NOT RUN", text), (
            f"{name} has no NOT RUN box. Committing 'not run' honestly must be as easy as "
            "committing a pass, or the pressure is to claim the pass."
        )

    def test_no_signature_is_pre_filled(self, name: str) -> None:
        for headers, cells, line in table_rows(read(name)):
            for header, cell in zip(headers, cells):
                if "signed off by" in cells[0].lower() and header == "value":
                    assert cell == BLANK, (
                        f"{name}:{line} pre-fills a sign-off: {cell!r}"
                    )


# ==================================================================================================
@pytest.mark.parametrize("name", GATE_FILES)
class TestEachGateQuotesThePlaybook:
    def test_the_pass_condition_is_quoted_verbatim(self, name: str) -> None:
        expected = next(cond for n, _, cond in GATES if n == name)
        assert expected in read(name), (
            f"{name} does not quote its playbook 6.1 pass condition verbatim. A paraphrased pass "
            f"condition drifts from the binding one. Expected: {expected!r}"
        )

    def test_the_owner_is_named(self, name: str) -> None:
        expected = next(owner for n, owner, _ in GATES if n == name)
        assert expected in read(name), f"{name} does not name its owner ({expected})"

    def test_the_failure_response_is_stated(self, name: str) -> None:
        expected = FAILURE_RESPONSES[name]
        assert expected in read(name), (
            f"{name} does not state what happens on failure. Expected: {expected!r}. A gate with "
            "no stated consequence is a checklist item."
        )

    def test_it_declares_what_it_does_not_establish(self, name: str) -> None:
        text = read(name)
        assert "does not establish" in text, (
            f"{name} has no limits section. Every gate here is narrower than the claim somebody "
            "will make from it, and the gap belongs in the report."
        )

    def test_predeclared_criteria_come_before_results(self, name: str) -> None:
        text = read(name)
        criteria = text.find("Predeclared")
        results = text.find("Results")
        assert criteria != -1, f"{name} has no predeclared-criteria section"
        assert results != -1, f"{name} has no results section"
        assert criteria < results, (
            "criteria declared after results are not criteria, they are a description of the "
            "results; the file order is part of the control"
        )

    def test_it_says_which_section_is_filled_before_running(self, name: str) -> None:
        assert "BEFORE running" in read(name) or "BEFORE inspecting" in read(name)

    def test_it_records_the_artifact_hashes_it_judges(self, name: str) -> None:
        # A report that does not identify what it measured cannot be matched to an artifact later,
        # and an unattributable pass is indistinguishable from a pass on something else.
        text = read(name).lower()
        assert "sha-256" in text or "sha256" in text, (
            f"{name} records no artifact digest"
        )
        assert "commit" in text, f"{name} records no source commit"


# ==================================================================================================
class TestTheTwoBlockers:
    """ONNX parity blocks deployment; privacy blocks the release. Pinned so a softening edit fails."""

    def test_onnx_parity_is_a_deploy_blocker(self) -> None:
        text = read("gate-5-onnx-parity.md")
        assert "DEPLOY BLOCKER" in text
        assert "Block deployment artifact" in text
        assert "may not be deployed" in text

    def test_onnx_parity_not_run_counts_as_failed_for_deployment(self) -> None:
        # The gap this closes: "we never ran it" is not a pass, and an unrun gate is the most likely
        # state on the day of a deadline.
        text = read("gate-5-onnx-parity.md")
        assert re.search(r"NOT RUN.*treated as FAIL", text, re.IGNORECASE | re.DOTALL)

    def test_onnx_parity_forbids_widening_the_tolerance_after_the_fact(self) -> None:
        text = read("gate-5-onnx-parity.md")
        assert "Do not widen" in text, (
            "the predeclared tolerance is the whole control; a tolerance widened to fit the "
            "observed deviation is a description of the deviation"
        )
        assert "predeclar" in text.lower()

    def test_onnx_parity_checks_class_orientation_explicitly(self) -> None:
        # The failure that is invisible everywhere else: an inverted head still returns in-range
        # scores, still produces actions, and still leaves a verifiable audit chain.
        text = read("gate-5-onnx-parity.md")
        assert "orientation" in text.lower()
        assert "inversion" in text.lower()

    def test_privacy_is_a_release_blocker(self) -> None:
        text = read("gate-7-privacy.md")
        assert "RELEASE BLOCKER" in text
        assert "Block demo release" in text

    def test_privacy_failure_blocks_the_demo_too(self) -> None:
        text = read("gate-7-privacy.md")
        assert "INCLUDING THE DEMO" in text.upper(), (
            "a private audience is still an audience; the harm from an exposed waveform does not "
            "scale with the size of the room"
        )
        assert "no partial pass" in text.lower()

    def test_privacy_not_run_counts_as_failed(self) -> None:
        text = read("gate-7-privacy.md")
        assert re.search(r"NOT RUN.*treated as FAIL", text, re.IGNORECASE | re.DOTALL)

    def test_privacy_thresholds_are_zero_and_not_negotiable(self) -> None:
        text = read("gate-7-privacy.md")
        assert "Permitted findings of any severity | zero" in text, (
            "this is the one gate with no tunable criterion; a permitted-findings row that can be "
            "filled in is a row somebody fills in"
        )

    def test_privacy_is_inspected_after_the_system_is_exercised(self) -> None:
        text = read("gate-7-privacy.md")
        assert "after" in text.lower() and "empty database" in text.lower(), (
            "an unexercised system passes every check in this gate and proves nothing"
        )

    def test_the_readme_explains_why_the_two_blockers_differ(self) -> None:
        text = read("README.md")
        assert "DEPLOY BLOCKER" in text and "RELEASE BLOCKER" in text
        assert "deploy blocker" in text.lower() or "Blocks" in text
        assert "not recoverable by rolling back" in prose("README.md"), (
            "the distinction is the point: a bad artifact can be withdrawn, an exposed waveform "
            "cannot"
        )

    def test_no_other_gate_claims_to_be_a_blocker(self) -> None:
        # Six gates restrict claims rather than halting the pipeline. If everything is a blocker,
        # the two that really are stop meaning anything.
        for name in GATE_FILES:
            if name in ("gate-5-onnx-parity.md", "gate-7-privacy.md"):
                continue
            text = read(name)
            assert "DEPLOY BLOCKER" not in text, f"{name} claims to be a deploy blocker"
            assert "RELEASE BLOCKER" not in text, (
                f"{name} claims to be a release blocker"
            )
            assert "Not a deploy or release blocker" in text, (
                f"{name} must say what it does NOT block, so a reader does not assume it halts "
                "the pipeline"
            )


# ==================================================================================================
class TestGateContentTheProjectDependsOn:
    """A handful of specific rows that other parts of this repository rely on existing."""

    def test_the_data_gate_checks_the_grouping_key_disjointness_invariant(self) -> None:
        text = read("gate-1-data.md")
        assert "grouping_key_sha256" in text
        assert "more than one split" in text, (
            "this is THE split-protocol check; datasets/manifest/manifest.schema.json precomputes "
            "the key precisely so it is a group-by over one column"
        )

    def test_the_data_gate_checks_consent_and_withdrawal(self) -> None:
        text = read("gate-1-data.md")
        assert "retention_expiry" in text
        assert "Withdrawn" in text
        assert "surviving manifest samples" in text, (
            "a withdrawal that leaves the augmented children behind is half-honoured, and that is "
            "the likely failure rather than ignoring the request outright"
        )

    def test_the_calibration_gate_pins_the_split_rule(self) -> None:
        text = read("gate-4-calibration.md")
        assert "R-37" in text
        assert "eval_locked" in text and "dev_calibration" in text
        assert "Number of times `eval_locked` has been read" in text, (
            "'we peeked once to check' is how a locked split is spent; counting the reads is the "
            "only way the peek leaves a trace"
        )

    def test_the_calibration_gate_gates_probability_language(self) -> None:
        text = read("gate-4-calibration.md")
        assert "R-11" in text
        assert "probability" in text.lower()

    def test_the_calibration_gate_names_the_threshold_derivation_it_would_permit(
        self,
    ) -> None:
        text = read("gate-4-calibration.md")
        assert "cost" in text.lower() and "per use case" in text.lower()
        assert "derivation" in text.lower()

    def test_the_privacy_gate_checks_the_exact_allow_list_both_directions(self) -> None:
        text = read("gate-7-privacy.md")
        assert "equal the declared allow-list exactly" in text
        assert (
            "absent from the contract" in text and "absent from the database" in text
        ), (
            "a subset check passes happily after someone adds a column, which is exactly the "
            "migration this gate has to catch"
        )

    def test_the_privacy_gate_checks_logs_not_only_the_database(self) -> None:
        text = read("gate-7-privacy.md")
        for surface in (
            "debug level",
            "traceback",
            "Validation-error",
            "Metrics labels",
        ):
            assert surface in text, (
                f"{surface} is not inspected; logs are where this gate fails"
            )

    def test_the_privacy_gate_checks_the_pseudonym_and_the_raw_reference(self) -> None:
        text = read("gate-7-privacy.md")
        assert "client_call_ref" in text and "R-16" in text
        assert "64-lowercase-hex" in text

    def test_the_privacy_gate_checks_the_retention_receipt_carries_no_personal_data(
        self,
    ) -> None:
        text = read("gate-7-privacy.md")
        assert "Retention receipt contains no personal data" in text
        assert "whole sessions atomically" in text, (
            "the retention design is whole-session deletion; a gate that does not check it cannot "
            "tell a swept chain from a broken one"
        )

    def test_the_privacy_gate_forbids_chain_key_rotation(self) -> None:
        """R-58, not R-31. R-31 is the ASG-zeroing rule and has nothing to do with the chain key.

        This assertion used to read ``"R-31" in read(...)`` — a citation to a rule that says
        something else, for a prohibition that at the time no rule of record contained at all
        (memory.md §4 BUG-20). R-58 was added to rules.md to close that gap. Asserting the rule ID
        is still a weak check: it survives the row being reworded to anything that mentions R-58.
        What it does buy is that the citation a reviewer follows lands on text that states the
        prohibition, which is what failed before.
        """
        assert "R-58" in read("gate-7-privacy.md")

    def test_the_demo_gate_allows_presenting_one_tier_truthfully(self) -> None:
        text = read("gate-8-demo.md")
        assert "present one tier only, truthfully" in text
        assert "PASS (one tier, stated truthfully)" in text, (
            "if the only pass is a two-tier pass, an unmeasured fallback latency gets quoted from "
            "the GPU tier"
        )

    def test_the_demo_gate_requires_disclosing_a_mock_detector(self) -> None:
        assert "mock" in read("gate-8-demo.md").lower()
        assert "before the first score appears" in prose("gate-8-demo.md")

    def test_the_ood_gate_cannot_be_failed_by_a_bad_number(self) -> None:
        text = read("gate-3-ood.md")
        assert "it is to REPORT" in text
        assert "failed by an unreported cohort" in text, (
            "the playbook's pass condition is to report the held-out results, not to score well; "
            "reading it as a quality bar creates pressure to omit the cohort that degrades"
        )

    def test_the_ood_gate_forbids_merging_small_cohorts_away(self) -> None:
        text = read("gate-3-ood.md")
        assert "insufficient-data" in text
        assert "never merged" in text or "never omitted" in text

    def test_the_ood_gate_reports_the_pooled_figure_last(self) -> None:
        text = read("gate-3-ood.md")
        assert text.find("Worst group") < text.find("Aggregate, for reference only"), (
            "the pooled figure is the one that gets quoted and means least; the worst-group figure "
            "goes first so it is the one a reader sees"
        )

    def test_the_ood_gate_only_uses_legitimately_labelled_cohort_metadata(self) -> None:
        text = read("gate-3-ood.md")
        assert "accent_region_source" in text
        assert "never inferred from audio" in text

    def test_the_quantization_gate_treats_the_artifact_as_a_new_version(self) -> None:
        text = read("gate-6-quantization.md")
        assert "separate model version" in text
        assert "must differ from FP32" in text
        assert "inherits nothing" in prose("gate-6-quantization.md")

    def test_the_quantization_gate_checks_distribution_compression(self) -> None:
        text = read("gate-6-quantization.md")
        assert "Fraction of windows over the policy threshold" in text, (
            "a quantized model can hold its EER and move this fraction substantially, changing "
            "every action without changing an accuracy metric"
        )

    def test_the_baseline_gate_checks_class_orientation_and_the_input_pipeline(
        self,
    ) -> None:
        text = read("gate-2-baseline.md")
        assert "class orientation" in text.lower()
        assert "Input pipeline verification" in text

    def test_the_baseline_gate_requires_multiple_seeds(self) -> None:
        text = read("gate-2-baseline.md")
        assert "mean and range" in text
        assert "Seeds are plural" in text

    def test_the_baseline_gate_refuses_to_fabricate_asv_parameters(self) -> None:
        text = read("gate-2-baseline.md")
        assert "not-applicable" in text and "t-DCF" in text, (
            "min t-DCF without the official ASV parameters is a number with no meaning, and "
            "playbook 6 forbids fabricating them"
        )


# ==================================================================================================
class TestTheReadme:
    def test_it_states_the_no_numbers_rule(self) -> None:
        text = read("README.md")
        assert "No pre-filled numbers" in text
        assert "___" in text

    def test_it_points_at_the_test_that_enforces_the_rule(self) -> None:
        assert "test_evaluation_templates.py" in read("README.md"), (
            "a documented rule with no named enforcement is a convention"
        )

    def test_it_requires_criteria_to_be_committed_before_results(self) -> None:
        text = read("README.md")
        assert "second commit" in text or "before running" in text.lower()
        assert "reject" in text.lower()

    def test_it_says_not_run_and_fail_are_committable(self) -> None:
        text = read("README.md")
        assert "`not-run` and `fail` are both legitimate" in text, (
            "if only a pass is committable, the pressure is on the report rather than the system"
        )

    def test_it_does_not_promote_the_current_artifact_state(self) -> None:
        text = read("README.md")
        assert "demo_eligible" in text
        assert "placeholder" in text
        assert "no single gate promotes" in text.lower()

    def test_the_templates_are_marked_as_the_control(self) -> None:
        text = read("README.md")
        assert "Do not edit the template in place" in text


# ==================================================================================================
class TestTheParserItself:
    """If table_rows() finds nothing, every blankness test above is vacuous."""

    def test_it_finds_rows_in_every_template(self) -> None:
        for name in GATE_FILES:
            rows = table_rows(read(name))
            assert len(rows) > 10, f"{name}: parsed only {len(rows)} table rows"

    def test_it_finds_value_columns_in_every_template(self) -> None:
        for name in GATE_FILES:
            found = [
                (h, c)
                for headers, cells, _ in table_rows(read(name))
                for h, c in zip(headers, cells)
                if h in VALUE_COLUMNS
            ]
            assert found, (
                f"{name}: no value column recognised, so blankness was never checked"
            )

    def test_it_would_catch_a_filled_cell(self) -> None:
        filled = "| Field | Value |\n|---|---|\n| Scorer latency p95 | 42 |\n"
        rows = table_rows(filled)
        assert rows and rows[0][1][1] == "42"

    def test_it_skips_the_separator_row(self) -> None:
        rows = table_rows("| A | B |\n|---|---|\n| 1 | 2 |\n")
        assert len(rows) == 1

    def test_it_resets_headers_between_tables(self) -> None:
        # Two tables with different headers, separated by prose. Without the reset, the second
        # table's cells would be checked against the first table's column names.
        text = "| Field | Value |\n|---|---|\n| a | `___` |\n\ntext\n\n| X | Y |\n|---|---|\n| b | c |\n"
        rows = table_rows(text)
        assert [r[0] for r in rows] == [["field", "value"], ["x", "y"]]
