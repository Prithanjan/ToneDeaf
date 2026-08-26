"""Platt scaling: monotonicity, the [0,1] guarantee, and the three refusals that block a bad artifact.

The reason these are tests and not just runtime checks: every failure mode below produces numbers that
look entirely reasonable. An inverted detector returns values in [0,1]. A zero slope returns values in
[0,1]. A transform fitted on the locked evaluation set returns values in [0,1] and better-looking metrics
than the honest one. Nothing about the output reveals the problem.
"""

from __future__ import annotations

import itertools
import json
import math
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path

import pytest

from app.calibration import (
    FITTED_STATUS,
    PLACEHOLDER_STATUS,
    REQUIRED_FIT_SPLIT,
    Calibration,
    CalibrationError,
    load_calibration,
    placeholder_calibration,
)


class TestPlattTransform:
    """``spoof_risk = sigmoid(slope * raw + intercept)``, bounded and monotone."""

    def test_output_is_in_the_unit_interval(self, fitted_calibration: Calibration) -> None:
        """Prevents a value outside [0,1] reaching a numeric(5,4) audit column and a threshold compare."""
        for raw in (-1e12, -1e3, -10.0, -1.0, 0.0, 1.0, 10.0, 1e3, 1e12):
            risk = fitted_calibration.apply(raw)
            assert 0.0 <= risk <= 1.0

    def test_transform_is_strictly_monotone_increasing(
        self, fitted_calibration: Calibration
    ) -> None:
        """Prevents an inverted or flat mapping, both of which return plausible probabilities.

        An inverted detector holds payments on bona-fide callers and clears the synthetic ones, and it
        looks exactly as healthy as a correct one from every metric the system reports.
        """
        raws = [-8.0, -4.0, -1.0, -0.25, 0.0, 0.25, 1.0, 4.0, 8.0]
        risks = [fitted_calibration.apply(raw) for raw in raws]
        assert risks == sorted(risks)
        assert all(a < b for a, b in itertools.pairwise(risks))
        assert fitted_calibration.is_monotone_increasing

    def test_extreme_logits_saturate_without_overflowing(
        self, fitted_calibration: Calibration
    ) -> None:
        """Prevents an OverflowError, or an overflow warning becoming an error under filterwarnings.

        math.exp overflows past |z| ~ 710. The logit is clamped rather than the probability, so the
        transform stays monotone all the way out instead of developing a flat region before saturation.
        """
        assert fitted_calibration.apply(1e300) == pytest.approx(1.0)
        assert fitted_calibration.apply(-1e300) == pytest.approx(0.0)

    def test_midpoint_matches_the_closed_form(self) -> None:
        """Prevents a transposed slope/intercept, which would still be monotone and still be in [0,1]."""
        calibration = placeholder_calibration()
        assert calibration.apply(0.0) == pytest.approx(0.5)
        assert calibration.apply(1.0) == pytest.approx(1.0 / (1.0 + math.exp(-1.0)))

    def test_non_finite_raw_score_is_refused(self, fitted_calibration: Calibration) -> None:
        """Prevents NaN silently disabling the k-of-n rule.

        Every comparison against NaN is false, so a NaN spoof_risk would make ``risk >= threshold``
        false for every window. The evidence counter would stop incrementing and the session would look
        clean — a detector that has stopped detecting while still reporting.
        """
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(CalibrationError):
                fitted_calibration.apply(bad)


class TestArtifactLoading:
    """What ``calibration.json`` must contain before this service will serve from it."""

    def test_valid_artifact_loads_and_is_hashed(
        self,
        fitted_calibration_document: dict[str, object],
        write_calibration: Callable[..., Path],
    ) -> None:
        """The SHA-256 must be of the FILE, so the parity set names something a reviewer can verify."""
        path = write_calibration(fitted_calibration_document)
        calibration = load_calibration(path)
        assert calibration.sha256 == sha256(path.read_bytes()).hexdigest()
        assert calibration.version == "1.0.0-test"
        assert calibration.is_policy_eligible

    def test_missing_artifact_raises_rather_than_defaulting(self, tmp_path: Path) -> None:
        """Prevents an invented slope and intercept standing in for a fitted one.

        There is deliberately no default transform in the module. A missing artifact means nobody has
        mapped this model's output onto a risk, and producing a number anyway is how a demo ends up
        reporting values against a curve that was never fitted.
        """
        with pytest.raises(CalibrationError):
            load_calibration(tmp_path / "absent.json")

    def test_malformed_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "calibration.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(CalibrationError):
            load_calibration(path)

    @pytest.mark.parametrize(
        "missing",
        [
            "status",
            "fitted_on",
            "method",
            "slope",
            "intercept",
            "calibration_version",
            "model_version",
            "model_sha256",
        ],
    )
    def test_every_required_key_is_required(
        self,
        fitted_calibration_document: dict[str, object],
        write_calibration: Callable[..., Path],
        missing: str,
    ) -> None:
        """Prevents a partially-written artifact loading with implicit defaults for the rest."""
        document = dict(fitted_calibration_document)
        del document[missing]
        with pytest.raises(CalibrationError):
            load_calibration(write_calibration(document))

    def test_unknown_status_is_refused(
        self,
        fitted_calibration_document: dict[str, object],
        write_calibration: Callable[..., Path],
    ) -> None:
        """Prevents a typo in ``status`` promoting a placeholder to policy-eligible.

        The dangerous default is "unknown means fine": if an unrecognized status were treated as fitted,
        then ``"placeholder-not-policy-elligible"`` — one letter wrong — would silently pass the R-11
        gate. So the status must be one of exactly two known values.
        """
        document = dict(fitted_calibration_document)
        document["status"] = "fitted"
        with pytest.raises(CalibrationError, match="unknown calibration status"):
            load_calibration(write_calibration(document))

    @pytest.mark.parametrize("method", ["isotonic", "temperature", "beta", "none", "PLATT"])
    def test_unsupported_method_is_refused(
        self,
        fitted_calibration_document: dict[str, object],
        write_calibration: Callable[..., Path],
        method: str,
    ) -> None:
        """Prevents another method's parameters being read as a slope and an intercept.

        Isotonic regression, temperature scaling, and beta calibration are all defensible, and none of
        them is ``slope * x + intercept``. Feeding their parameters through this transform produces a
        plausible curve that is not the fitted one.
        """
        document = dict(fitted_calibration_document)
        document["method"] = method
        with pytest.raises(CalibrationError):
            load_calibration(write_calibration(document))

    def test_zero_slope_is_refused(
        self,
        fitted_calibration_document: dict[str, object],
        write_calibration: Callable[..., Path],
    ) -> None:
        """Prevents a constant risk being served by a service that reports itself healthy."""
        document = dict(fitted_calibration_document)
        document["slope"] = 0.0
        with pytest.raises(CalibrationError):
            load_calibration(write_calibration(document))

    def test_negative_slope_is_refused(
        self,
        fitted_calibration_document: dict[str, object],
        write_calibration: Callable[..., Path],
    ) -> None:
        """Prevents fixing an inverted export by flipping the calibration sign.

        If the model's class orientation is inverted, the fix belongs at export (playbook §7) where it is
        recorded in the artifact. A negative slope hides the inversion inside a JSON file that reads as a
        perfectly ordinary calibration.
        """
        document = dict(fitted_calibration_document)
        document["slope"] = -2.5
        with pytest.raises(CalibrationError, match="inverts the detector"):
            load_calibration(write_calibration(document))

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), "2.5abc", None, [], {}])
    def test_non_numeric_parameters_are_refused(
        self,
        fitted_calibration_document: dict[str, object],
        write_calibration: Callable[..., Path],
        value: object,
    ) -> None:
        document = dict(fitted_calibration_document)
        document["slope"] = value
        path = write_calibration(dict(document))
        # json.dumps writes NaN/Infinity as bare tokens; json.loads accepts them back as floats, so the
        # finiteness check in the loader is the thing being exercised, not the JSON parser.
        assert path.read_text(encoding="utf-8")
        with pytest.raises(CalibrationError):
            load_calibration(path)


@pytest.mark.parity
class TestEvalLockedRefusal:
    """rules.md R-37: Platt scaling is fitted on ``dev_calibration`` and nothing else."""

    @pytest.mark.parametrize("split", ["eval_locked", "train"])
    def test_forbidden_split_is_refused_by_name(
        self,
        fitted_calibration_document: dict[str, object],
        write_calibration: Callable[..., Path],
        split: str,
    ) -> None:
        """Prevents the one honest evaluation the release is allowed being spent on threshold tuning.

        Contamination is invisible afterwards: the resulting numbers look BETTER, and nothing in the
        artifact records why. The refusal is enforced on the serving tier as well as in the training
        repo, because this is the last point at which the mistake is still cheap.
        """
        document = dict(fitted_calibration_document)
        document["fitted_on"] = split
        with pytest.raises(CalibrationError, match="R-37"):
            load_calibration(write_calibration(document))

    def test_any_other_split_is_also_refused(
        self,
        fitted_calibration_document: dict[str, object],
        write_calibration: Callable[..., Path],
    ) -> None:
        """Prevents an unrecognized split name passing because it is not on the forbidden list.

        An allow-list of one, not a deny-list. ``eval_holdout``, ``test``, and ``dev`` are all names
        someone would plausibly write, and none of them is the split R-37 permits.
        """
        document = dict(fitted_calibration_document)
        document["fitted_on"] = "eval_holdout"
        with pytest.raises(CalibrationError):
            load_calibration(write_calibration(document))

    def test_required_split_name_is_pinned(self) -> None:
        assert REQUIRED_FIT_SPLIT == "dev_calibration"


@pytest.mark.privacy
class TestPlaceholderCalibration:
    """rules.md R-11: loadable, servable, and never policy-eligible."""

    def test_placeholder_is_loadable(self) -> None:
        """Prevents the Phase-1 Compose tier being blocked on Pair B's Phase-2 artifact.

        ``docker compose up`` and the Gateway's WSS contract suite are Phase-1 exit criteria while
        ``policy/calibration.json`` is a Phase-2 deliverable. Refusing to start without it would block
        two pairs on an artifact two days out.
        """
        calibration = placeholder_calibration()
        assert calibration.status == PLACEHOLDER_STATUS
        assert calibration.apply(0.0) == pytest.approx(0.5)

    def test_placeholder_is_never_policy_eligible(self) -> None:
        """Prevents a placeholder-calibrated score being described as a probability.

        This is the single predicate that gates probability language in the UI, the logs, and the docs,
        and that CI checks before a release. A placeholder that answered True here would make every one
        of those checks pass on an uncalibrated number.
        """
        assert placeholder_calibration().is_policy_eligible is False

    def test_placeholder_labels_itself_in_every_string_field(self) -> None:
        """Prevents a placeholder's version string being mistaken for a real one in an audit row.

        Each of these values reaches the health response, the startup banner, and the audit table. A
        neutral value like "1.0.0" in any of them would survive into a screenshot with nothing to
        contradict it.
        """
        calibration = placeholder_calibration()
        assert "placeholder" in calibration.version
        assert "placeholder" in calibration.status
        assert "not-a-detector" in calibration.model_version
        assert calibration.model_sha256 == sha256(b"").hexdigest()

    def test_placeholder_declares_no_contract_vector_expectation(self) -> None:
        """Prevents a fabricated expected score turning the startup parity check into a rubber stamp."""
        assert placeholder_calibration().contract_vector_raw_score is None

    def test_a_file_with_placeholder_status_loads_but_stays_ineligible(
        self,
        fitted_calibration_document: dict[str, object],
        write_calibration: Callable[..., Path],
    ) -> None:
        """The required behaviour, on the path that matters: an on-disk artifact, not the built-in.

        Pair B's Day-1 ``policy/calibration.json`` will carry the placeholder status with real-looking
        slope and intercept values. It must LOAD — otherwise the demo cannot run — and it must never
        make this service claim policy eligibility.
        """
        document = dict(fitted_calibration_document)
        document["status"] = PLACEHOLDER_STATUS
        calibration = load_calibration(write_calibration(document))
        assert calibration.slope == 2.5  # the numbers are honoured
        assert calibration.apply(1.0) > 0.0  # the service can serve
        assert calibration.is_policy_eligible is False  # and still cannot claim eligibility

    def test_eligibility_predicate_matches_the_gateway_exactly(self) -> None:
        """Prevents the two services disagreeing about whether the same file is policy-eligible.

        Two services with different answers is worse than either answer alone: the Gateway would
        suppress probability language while the Scorer advertised policy eligibility in its health
        response, and the release manifest would then contain both claims. Asserted by comparing the
        Gateway's constant as TEXT, since the Scorer cannot import it (see test_contract.py).
        """
        from tests.conftest import REPO_ROOT

        gateway_loader = (REPO_ROOT / "gateway" / "app" / "policy" / "loader.py").read_text(
            encoding="utf-8"
        )
        assert f'PLACEHOLDER_STATUS: Final[str] = "{PLACEHOLDER_STATUS}"' in gateway_loader
        assert "status != PLACEHOLDER_STATUS" in gateway_loader


class TestContractVectorExpectation:
    """The optional expected raw score for ``ml/fixtures/contract_vector_v1.npy``."""

    def test_absent_expectation_is_none_not_zero(
        self,
        fitted_calibration_document: dict[str, object],
        write_calibration: Callable[..., Path],
    ) -> None:
        """Prevents a missing expectation being read as an expected score of 0.0.

        0.0 is a perfectly plausible raw score, so defaulting to it would turn the every-startup parity
        check into an assertion that the model outputs zero — which would then fail on a correct build
        and be "fixed" by widening the tolerance.
        """
        calibration = load_calibration(write_calibration(fitted_calibration_document))
        assert calibration.contract_vector_raw_score is None

    def test_declared_expectation_is_read(
        self,
        fitted_calibration_document: dict[str, object],
        write_calibration: Callable[..., Path],
    ) -> None:
        document = dict(fitted_calibration_document)
        document["contract_vector_raw_score"] = -0.3125
        calibration = load_calibration(write_calibration(document))
        assert calibration.contract_vector_raw_score == -0.3125

    def test_non_finite_expectation_is_refused(
        self,
        fitted_calibration_document: dict[str, object],
        tmp_path: Path,
    ) -> None:
        document = dict(fitted_calibration_document)
        document["contract_vector_raw_score"] = float("nan")
        path = tmp_path / "calibration.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(CalibrationError):
            load_calibration(path)


def test_status_constants_are_not_secrets() -> None:
    """rules.md R-34: nothing in this module looks like a credential.

    ``FITTED_STATUS`` and ``PLACEHOLDER_STATUS`` are the only two string constants that travel outside
    the process, and both are descriptive English. A hex-looking or high-entropy status value would be
    flagged by a secret scanner and, worse, would be indistinguishable from one in a log.
    """
    for value in (FITTED_STATUS, PLACEHOLDER_STATUS):
        assert value.replace("-", "").isalpha()
