"""The committed ``policy/calibration.json``, put through the loader this service actually runs.

Every other calibration test in this directory works from the synthetic document at
``tests/conftest.py:75-86``. That fixture was written in the flat shape the loader wants, so it could
never disagree with the file on disk — and for several days it did not have to, because nothing
anywhere loaded the file on disk through this module at all.

That gap had a cost, recorded as memory.md §4 BUG-8. ``policy/calibration.json`` is mounted into BOTH
tiers (``infra/compose/docker-compose.yml`` mounts ``../../policy:/policy:ro`` into the Scorer as well
as the Gateway, with ``CALIBRATION_PATH: /policy/calibration.json``), and the two loaders require
DISJOINT key sets: ``gateway/app/policy/loader.py:139`` wants ``calibration_version``, ``status``,
``model_version``, ``model_sha256`` and never looks at a coefficient, while ``app/calibration.py``
wants top-level ``status``, ``method``, ``slope``, ``intercept``, and ``fitted_on``. The artifact
satisfied the Gateway's set only. ``app/server.py:201`` falls back to the built-in placeholder ONLY
when the file does not exist, so a file that is present and wrong is fatal rather than degraded: the
Scorer refused to start and ``docker compose up`` could not bring the tier up.

Two careful suites stayed green through all of it. The Gateway's asserted the keys the Gateway reads.
This one asserted this service's transform against this service's fixture. Neither ever put the real
bytes through the real loader, so there was no assertion in which the two could contradict each other.
That is the whole subject of this module: it is the cheapest gate that would have failed on day one,
and every test in it is written against the committed file rather than a stand-in for it.

Two things this module deliberately does NOT do. It does not re-check the loader's general refusals —
missing keys, unknown status, unsupported methods, bad slopes — which ``test_calibration.py`` already
covers against a synthetic document; the negative cases below are the ONE historical shape and the
keys that were actually absent from it. And it does not skip. A skip here reproduces the exact class of
failure the module exists to prevent: a gate that reports green while checking nothing.

These tests pin the artifact's CURRENT placeholder state on purpose, so several of them will fail on
the commit that lands a fitted calibration. That is the intended signal rather than a maintenance
burden — promotion away from ``placeholder-not-policy-eligible`` is a reviewed, checklist-driven event
(``policy/calibration.json::promotion_checklist`` step 6, rules.md R-11), and this suite is one of the
places that has to be looked at when it happens.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest

from app.calibration import (
    METHOD_PLATT,
    PLACEHOLDER_STATUS,
    REQUIRED_FIT_SPLIT,
    Calibration,
    CalibrationError,
    load_calibration,
    placeholder_calibration,
)
from app.model import MOCK_LOGIT_RANGE
from tests.conftest import REPO_ROOT

#: The two real files, located from ``REPO_ROOT`` — which ``tests/conftest.py`` derives from its own
#: ``__file__``. Imported rather than recomputed here: a second definition of the repo root is a second
#: thing to be wrong when the tree moves, and ``test_packaging.py`` already reads it from the same
#: place. Neither file is ever written to by this module; the negative cases copy to ``tmp_path``.
CALIBRATION_PATH: Final[Path] = REPO_ROOT / "policy" / "calibration.json"
POLICY_YAML_PATH: Final[Path] = REPO_ROOT / "policy" / "policy.yaml"

#: What ``gateway/app/policy/loader.py:139`` requires, and the only keys it reads. Named here as
#: STRINGS: importing that module needs ``gateway/`` on ``sys.path`` and pulls in PyYAML, which is not
#: a Scorer dependency, and the Scorer image contains no ``gateway/`` tree at all. The same reasoning,
#: written out at length, governs ``test_contract.py::TestGatewayConstantsParity``. The check that runs
#: the Gateway's own loader over this file lives in ``audit/tests/test_policy_bundle.py:375-377``.
GATEWAY_REQUIRED_KEYS: Final[tuple[str, ...]] = (
    "calibration_version",
    "status",
    "model_version",
    "model_sha256",
)

#: The top-level keys ``app/calibration.py`` requires that the Gateway's loader never looks at. These
#: four are exactly the ones that were missing from the committed artifact, which is why one tier
#: loaded it happily and the other was fatal on startup (memory.md §4 BUG-8).
SCORER_ONLY_REQUIRED_KEYS: Final[tuple[str, ...]] = ("method", "slope", "intercept", "fitted_on")


def _committed_document() -> dict[str, Any]:
    """The committed artifact as a mutable dict, for building the shapes that must be REFUSED.

    Derived from the real file rather than transcribed, so a negative case cannot pass because the stub
    it was built from had drifted away from the artifact it claims to be a mutation of. Mutations are
    written to ``tmp_path`` by the ``write_calibration`` fixture; the real file is only ever read.
    """
    document = json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "policy/calibration.json is not a JSON object"
    return document


def _window_threshold() -> float:
    """``thresholds.high_window_risk`` from the committed policy bundle, read without PyYAML.

    The number is not a named constant anywhere in Python, so there is nothing to import.
    ``gateway/app/policy/engine.py:158`` compares ``spoof_risk >= self.thresholds.high_window_risk``,
    an ordinary dataclass field, and the value in that field comes from ``policy/policy.yaml:58`` by
    way of ``gateway/app/policy/loader.py:181``. The choice is therefore between hardcoding ``0.78``
    here — another copy of a number that ``policy.yaml`` marks in capitals as a placeholder that must
    be re-derived — and reading the file that owns it. Reading it means a re-derived operating point
    moves this test with it instead of stranding it against a stale figure.

    Read as text, for the reason ``audit/tests/test_policy_bundle.py`` gives for the same technique:
    PyYAML is not in ``scorer/requirements.txt``, and adding a package to the service's dependency set
    so that a test can read four characters is a poor trade.

    Comments are stripped first. ``policy.yaml`` discusses ``0.78`` in the prose above the key in order
    to disclaim it, and a reader that matched comment text could pick up the disclaimer as the value.
    """
    for line in POLICY_YAML_PATH.read_text(encoding="utf-8").splitlines():
        body = "" if line.lstrip().startswith("#") else line.split("#", 1)[0]
        match = re.match(r"\s+high_window_risk:\s*([0-9.]+)\s*$", body)
        if match:
            return float(match.group(1))
    raise AssertionError(
        "thresholds.high_window_risk was not found in policy/policy.yaml. Whether the `high` band is "
        "reachable cannot be established without it, so this fails rather than assuming a value."
    )


@pytest.fixture(scope="module")
def committed_calibration() -> Calibration:
    """The real artifact, through the real loader: no fixture document, no temp file, no stub."""
    return load_calibration(CALIBRATION_PATH)


@pytest.mark.contract
@pytest.mark.parity
class TestTheCommittedArtifactLoadsInThisService:
    """The one assertion whose absence let BUG-8 ship: the real bytes, the real loader."""

    def test_the_artifact_exists_at_the_path_both_tiers_are_pointed_at(self) -> None:
        """Prevents every other test in this module passing vacuously against a file that moved.

        ``app/server.py:201`` treats a MISSING file as the Phase-1 no-artifact case and falls back to
        the built-in placeholder, in mock mode, without complaint. So if this file were renamed or
        relocated, the Scorer would still start and the rest of this module would have nothing to load
        — which is the same silence the module was written to break.
        """
        assert CALIBRATION_PATH.is_file(), f"no calibration artifact at {CALIBRATION_PATH}"

    def test_the_committed_artifact_loads_through_this_services_own_loader(self) -> None:
        """The BUG-8 gate. For days this call raised ``CalibrationError`` on the committed file.

        Called directly rather than through the fixture so that the failure is reported as a FAILURE in
        this named test, not as a collection error smeared across the module. It is also the exact call
        ``app/server.py:207`` makes at startup, with the exact path Compose and the CDK task definitions
        mount, so a green result here is evidence about the serving path and not about a fixture.
        """
        calibration = load_calibration(CALIBRATION_PATH)
        assert calibration.method == METHOD_PLATT
        assert calibration.fitted_on == REQUIRED_FIT_SPLIT
        assert calibration.source_path == str(CALIBRATION_PATH)

    def test_the_keys_both_tiers_require_are_present_at_the_top_level(self) -> None:
        """Prevents the divergence returning in the other direction: valid here, fatal on the Gateway.

        One file, two loaders, disjoint requirements, and no single place that has to satisfy both. The
        four Gateway keys are asserted by name rather than by running its loader (see
        ``GATEWAY_REQUIRED_KEYS`` for why importing it from this suite is not on offer), and the four
        this service adds are asserted alongside them so the union is visible in one assertion.
        """
        document = _committed_document()
        for key in (*GATEWAY_REQUIRED_KEYS, *SCORER_ONLY_REQUIRED_KEYS):
            assert key in document, f"policy/calibration.json is missing top-level {key!r}"
        assert not set(GATEWAY_REQUIRED_KEYS) & set(SCORER_ONLY_REQUIRED_KEYS)


@pytest.mark.contract
class TestTheCommittedArtifactIsStillAnUnfittedPlaceholder:
    """rules.md R-11, asserted on the artifact that is actually deployed rather than on a fixture."""

    def test_the_committed_artifact_is_not_policy_eligible(
        self, committed_calibration: Calibration
    ) -> None:
        """Prevents an uncalibrated number acquiring probability language by shipping in this file.

        This single predicate gates probability language in the UI, the logs, and the docs, and CI
        checks it before a release. The artifact is a development placeholder with the identity
        transform; nothing about its output would reveal that, so the status string is the only thing
        holding the claim shut and it has to be asserted on the real bytes.
        """
        assert committed_calibration.status == PLACEHOLDER_STATUS
        assert committed_calibration.is_policy_eligible is False

    def test_the_coefficients_are_exactly_the_unfitted_identity_pair(
        self, committed_calibration: Calibration
    ) -> None:
        """Prevents a placeholder carrying plausible coefficients, which no consumer could detect.

        The artifact's own ``platt.identity_note`` names the coefficient pair as the placeholder's
        fingerprint under the operative transform, and notes honestly that no code asserts it. This is
        that code. A placeholder with, say, ``slope: 1.7`` and ``intercept: -0.4`` would alter every
        score in the system, produce a perfectly monotone curve in [0,1], load without complaint, and
        be indistinguishable from a fitted mapping by looking at any output — while ``fitted`` in the
        same file still said ``false``.

        A genuinely fitted pair will essentially never land on exactly ``(1.0, 0.0)``, so this
        assertion is also the thing that fails on the commit that lands a real calibration. That is
        intended: see the module docstring.
        """
        assert (committed_calibration.slope, committed_calibration.intercept) == (1.0, 0.0)
        assert committed_calibration.is_monotone_increasing

    def test_the_artifact_declares_no_contract_vector_expectation(
        self, committed_calibration: Calibration
    ) -> None:
        """Prevents a fabricated expected score turning the startup parity check into a rubber stamp.

        ``app/server.py::_check_contract_vector`` treats an absent expectation as parity UNVERIFIABLE
        and reports ``contract_vector_parity_ok = false``. A number invented for this key in a file
        that is paired with no model would instead be re-scored, compared, and reported as verified.
        """
        assert committed_calibration.contract_vector_raw_score is None

    def test_the_files_presence_does_not_change_behaviour_while_it_is_a_placeholder(
        self, committed_calibration: Calibration
    ) -> None:
        """The property the BUG-8 fix was chosen to preserve: two startup paths, one behaviour.

        The Compose tier mounts this file, so the Scorer loads it. A bare checkout in mock mode has no
        file and falls back to ``placeholder_calibration()``. While the artifact is an unfitted
        placeholder those two paths must score identically, otherwise a demo run and a local run
        disagree about every window for a reason that appears nowhere in either output.
        """
        built_in = placeholder_calibration()
        grid = [-MOCK_LOGIT_RANGE, -3.0, -1.0, 0.0, 1.0, 3.0, MOCK_LOGIT_RANGE]
        assert [committed_calibration.apply(raw) for raw in grid] == [
            built_in.apply(raw) for raw in grid
        ]
        assert committed_calibration.status == built_in.status
        assert committed_calibration.is_policy_eligible == built_in.is_policy_eligible


@pytest.mark.contract
@pytest.mark.parity
class TestTheHighBandIsReachableWithThisArtifact:
    """Whether the product has a function at all, expressed as arithmetic over three committed files.

    ``spoof_risk`` is compared against ``thresholds.high_window_risk`` with ``>=`` at
    ``gateway/app/policy/engine.py:158``. If the calibrated output cannot reach that threshold, the
    Gateway's ``high`` state never occurs, so the ``hold`` and ``escalate`` outcomes never occur
    either. Nothing reports an error in that world: the Scorer is healthy, every window gets a
    plausible risk, the state machine runs, and the demo only ever shows a clean session.

    No existing test spans the chain. ``test_model.py:124-135`` pins the mock's logit range against the
    mock, ``test_calibration.py`` pins the transform against a fixture, and the threshold lives in a
    third file that neither of them reads.
    """

    def test_the_window_threshold_is_readable_from_the_policy_bundle(self) -> None:
        """Prevents this class passing because the reader stopped finding anything.

        A parser that silently returns a default is worse than no parser: every reachability assertion
        below would then be about a number this repo does not use. ``_window_threshold`` raises rather
        than defaulting, and the bound asserted here is the same one ``PolicyThresholds.__post_init__``
        enforces on the value at load time.
        """
        threshold = _window_threshold()
        assert 0.0 < threshold < 1.0

    def test_the_mocks_highest_raw_score_clears_the_window_threshold(
        self, committed_calibration: Calibration
    ) -> None:
        """The reachability assertion, end to end: mock logit range, committed coefficients, threshold.

        ``app/model.py`` emits a LOGIT in ±``MOCK_LOGIT_RANGE`` specifically so that the calibrated
        output spans enough of [0,1] to drive the Gateway's k-of-n machine through all three risk
        states. That intent is only realised if the coefficients in the committed artifact preserve it,
        and until this test existed nothing checked that they did.
        """
        threshold = _window_threshold()
        top = committed_calibration.apply(MOCK_LOGIT_RANGE)
        assert top >= threshold, (
            f"the highest spoof_risk this artifact can produce from the mock's raw score range is "
            f"{top}, which does not reach the {threshold} window threshold: the Gateway's `high` "
            "state is unreachable and the session can only ever look clean"
        )

    def test_the_mocks_lowest_raw_score_stays_under_the_window_threshold(
        self, committed_calibration: Calibration
    ) -> None:
        """The other half of reachability: a mapping that is always high is equally useless.

        Asserted because ``top >= threshold`` alone is satisfied by a degenerate artifact that pushes
        every window over the line — which would trip the k-of-n rule on every session and read as a
        detector that works extremely well.
        """
        assert committed_calibration.apply(-MOCK_LOGIT_RANGE) < _window_threshold()

    def test_reading_raw_score_as_a_probability_would_put_the_band_out_of_reach(
        self, committed_calibration: Calibration
    ) -> None:
        """Pins the arithmetic behind the artifact's ``transform_divergence_note``.

        The artifact's ``platt.transform`` string declares ``sigmoid(a*logit(raw)+b)``, which treats
        ``raw_score`` as a PROBABILITY and round-trips it. ``app/calibration.py:121`` executes
        ``sigmoid(slope*raw + intercept)`` with no ``logit()``, which treats ``raw_score`` as a LOGIT.
        The second is operative. ``(1.0, 0.0)`` is the one point in parameter space where the two
        conventions produce the same two numbers while meaning different things, which is exactly how
        two green suites came to assert incompatible semantics without ever colliding.

        Under the probability reading the input is confined to [0,1], so the highest attainable risk is
        ``sigmoid(1.0)`` — 0.7310585786 — and it sits below the window threshold. That is the state
        this module exists to keep the artifact out of, and the assertion is written as the closed form
        rather than the decimal so it says which function is being ruled out.

        If this ever fails because the operating point was re-derived BELOW that value, the reasoning
        in ``transform_divergence_note`` and in ``app/model.py:64-69`` needs revisiting. It does not
        mean the assertion was wrong.
        """
        best_under_the_probability_reading = committed_calibration.apply(1.0)
        assert best_under_the_probability_reading == pytest.approx(1.0 / (1.0 + math.exp(-1.0)))
        assert best_under_the_probability_reading < _window_threshold()


@pytest.mark.contract
class TestTheShapeThatBrokeTheScorerIsStillRefused:
    """The historical shape, reconstructed from the real file, asserted to be rejected.

    Not a duplicate of ``test_calibration.py``'s missing-key and unsupported-method cases. Those work
    from a synthetic document that was always in the correct flat shape. These start from the bytes on
    disk and remove exactly what BUG-8 removed, which is the only version of the question that could
    have gone wrong — and did, for days, while both suites were green.
    """

    def test_the_shape_that_shipped_is_refused(
        self, write_calibration: Callable[..., Path]
    ) -> None:
        """Reproduces BUG-8 on a scratch copy and asserts the loader still refuses it.

        The pre-fix artifact carried ``method: "platt-scaling"`` and no top-level ``slope``,
        ``intercept``, or ``fitted_on`` — it described its coefficients in a nested ``platt`` block
        that ``app/calibration.py`` does not read. Every key the Gateway needs was present, so the
        Gateway loaded it, hashed it, and stamped its digest into audit rows while the Scorer could not
        start at all.

        Only the refusal is asserted, not which check fires first: the loader's ordering is incidental
        and pinning it here would make an unrelated reordering look like a regression.
        """
        document = _committed_document()
        document["method"] = "platt-scaling"
        for key in ("slope", "intercept", "fitted_on"):
            del document[key]
        with pytest.raises(CalibrationError):
            load_calibration(write_calibration(document))

    def test_the_hyphenated_method_name_alone_is_refused(
        self, write_calibration: Callable[..., Path]
    ) -> None:
        """One key, one word, and the Scorer will not start. Everything else stays as committed.

        ``method`` is compared by exact string against ``METHOD_PLATT``, and both spellings name the
        same technique in English, so this is the edit most likely to be reintroduced by someone
        writing the file from the description rather than from the loader.
        """
        document = _committed_document()
        document["method"] = "platt-scaling"
        with pytest.raises(CalibrationError, match="unsupported calibration method"):
            load_calibration(write_calibration(document))

    @pytest.mark.parametrize("key", SCORER_ONLY_REQUIRED_KEYS)
    def test_each_key_the_gateway_does_not_read_is_load_bearing_here(
        self, write_calibration: Callable[..., Path], key: str
    ) -> None:
        """Prevents any one of the four being dropped as unused during a tidy-up of this file.

        None of these four appears anywhere in ``gateway/app/policy/loader.py``, and the artifact is
        long, heavily annotated, and looks Gateway-owned. Removing one would leave the Gateway's tests,
        the audit suite's assertions, and the policy bundle entirely green — and the Scorer unable to
        start. Asserting it per key means the failure message names the key that was lost.
        """
        document = _committed_document()
        del document[key]
        with pytest.raises(CalibrationError, match=re.escape(key)):
            load_calibration(write_calibration(document))
