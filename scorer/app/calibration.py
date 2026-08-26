"""Platt scaling — the only thing in this service that turns a model output into ``spoof_risk``.

``spoof_risk = sigmoid(slope * raw_score + intercept)``

Orientation is declared, not inferred: ``slope > 0`` means a higher raw score is more spoof-like. A
negative slope would invert the detector, and an inverted detector still produces plausible numbers in
[0,1] — it just holds payments on the bona-fide callers and lets the synthetic ones through. So the
sign is validated at load and the mapping's monotonicity has a test.

THREE GATES THIS MODULE ENFORCES

* **Fitted on ``dev_calibration``, never ``eval_locked`` (rules.md R-37).** The artifact must say
  which split it was fitted on, and any value other than ``dev_calibration`` refuses to load. Tuning
  a transform on the locked set spends the one honest evaluation the release is allowed, and the
  spend is invisible afterwards: the resulting numbers look better and nothing in the artifact records
  why. Enforcing it here means the refusal happens on the serving tier too, not only in the training
  repo where the split names live.
* **A score is not calibrated until an artifact says so (rules.md R-11).** While ``status`` is
  ``placeholder-not-policy-eligible`` the artifact still LOADS — the demo has to be able to run before
  a real calibration exists — but the service can never report ``policy_eligible``, so no probability
  language is permitted in UI, logs, or docs and CI blocks the release.
* **The pairing is checked.** ``model_sha256`` in the artifact must equal the SHA-256 of the ONNX file
  actually loaded (``technical-design.md`` §7 step 1). Thresholds fitted against one model's score
  distribution, applied to another's, is a mis-calibration with no symptom.

The eligibility predicate below is deliberately IDENTICAL to
``gateway/app/policy/loader.py::CalibrationArtifact.is_policy_eligible``. Two services that disagree
about whether the same file is policy-eligible is worse than either answer alone: the Gateway would
suppress probability language while the Scorer advertised ``policy_eligible`` in its health response,
and the release manifest would then contain both claims.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Final

#: Mirrors gateway/app/policy/loader.py. See the module docstring on why these must not diverge.
PLACEHOLDER_STATUS: Final[str] = "placeholder-not-policy-eligible"
FITTED_STATUS: Final[str] = "fitted-dev-calibration"

#: An unknown status refuses to load rather than being treated as one or the other. The dangerous
#: default is "unknown means fine": a typo in `status` would then silently promote a placeholder to
#: policy-eligible, which is the one transition rules.md R-11 exists to prevent.
VALID_STATUSES: Final[frozenset[str]] = frozenset({PLACEHOLDER_STATUS, FITTED_STATUS})

#: rules.md R-37. The only split a calibration transform may be fitted on.
REQUIRED_FIT_SPLIT: Final[str] = "dev_calibration"

#: rules.md R-37 again, from the other direction: name the split that must never appear, so the
#: refusal message can be specific about what went wrong.
FORBIDDEN_FIT_SPLITS: Final[frozenset[str]] = frozenset({"eval_locked", "train"})

METHOD_PLATT: Final[str] = "platt"

#: sigmoid saturates in float64 past |z| ~ 710 (exp overflows). Clamping the logit rather than the
#: probability keeps the transform monotone all the way out and avoids an overflow warning becoming an
#: exception under `filterwarnings = ["error"]`.
_LOGIT_CLAMP: Final[float] = 700.0


class CalibrationError(ValueError):
    """The calibration artifact is invalid or ineligible. Refuse to start."""


def _sigmoid(z: float) -> float:
    """Numerically stable logistic. Never overflows, never returns outside [0, 1]."""
    if z >= 0.0:
        return 1.0 / (1.0 + math.exp(-min(z, _LOGIT_CLAMP)))
    exp_z = math.exp(max(z, -_LOGIT_CLAMP))
    return exp_z / (1.0 + exp_z)


@dataclass(frozen=True, slots=True)
class Calibration:
    """A loaded, validated Platt transform plus the identity of the artifact it came from."""

    version: str
    sha256: str
    status: str
    method: str
    slope: float
    intercept: float
    model_version: str
    model_sha256: str
    fitted_on: str
    #: Expected ``raw_score`` for ``ml/fixtures/contract_vector_v1.npy``, when the artifact declares
    #: one. ``None`` means parity is UNVERIFIABLE, which is reported as
    #: ``contract_vector_parity_ok = false`` rather than assumed true (frame_contract.md §6).
    contract_vector_raw_score: float | None
    source_path: str

    @property
    def is_policy_eligible(self) -> bool:
        """Whether these numbers may be described as probabilities (rules.md R-11).

        Identical predicate to the Gateway's. Do not "improve" one side of it.
        """
        return self.status != PLACEHOLDER_STATUS

    @property
    def is_monotone_increasing(self) -> bool:
        return self.slope > 0.0

    def apply(self, raw_score: float) -> float:
        """Map a pre-calibration model output onto ``spoof_risk`` in [0, 1].

        The clamp at the end is belt-and-braces: ``_sigmoid`` cannot leave [0,1] mathematically, but
        this value is the ONLY field the policy engine may read, it is compared against a threshold,
        and it is written to a ``numeric(5,4)`` audit column. A NaN reaching that comparison would
        make every ``>=`` false, so the k-of-n rule would silently stop counting high windows and the
        session would look clean. Refusing NaN here turns that into an error at the one place that can
        still name it.
        """
        if not math.isfinite(raw_score):
            raise CalibrationError("raw_score is not finite")
        risk = _sigmoid(self.slope * raw_score + self.intercept)
        return min(1.0, max(0.0, risk))


def _require(data: dict[str, Any], key: str) -> Any:
    if key not in data:
        raise CalibrationError(f"calibration artifact missing required key: {key}")
    return data[key]


def _finite_float(data: dict[str, Any], key: str) -> float:
    raw = _require(data, key)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise CalibrationError(f"calibration artifact key is not a number: {key}") from exc
    if not math.isfinite(value):
        raise CalibrationError(f"calibration artifact key is not finite: {key}")
    return value


def load_calibration(path: Path) -> Calibration:
    """Load, validate, and hash ``calibration.json``.

    Raises rather than falling back to a default transform. There is deliberately no default slope and
    intercept in this file: a missing artifact means the Scorer does not know how to turn its model's
    output into a risk, and inventing a mapping silently is how a demo ends up reporting numbers
    against a curve nobody fitted. The only mapping this module will produce without an artifact is
    :func:`placeholder_calibration`, and that one is labelled as a placeholder in every field.
    """
    try:
        raw_bytes = path.read_bytes()
        data = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise CalibrationError(f"cannot read calibration artifact: {path}") from exc

    if not isinstance(data, dict):
        raise CalibrationError("calibration artifact must be a JSON object")

    status = str(_require(data, "status"))
    if status not in VALID_STATUSES:
        raise CalibrationError(
            f"unknown calibration status: {status!r}. Valid values are {sorted(VALID_STATUSES)}. "
            "An unrecognized status is not treated as eligible (rules.md R-11)."
        )

    fitted_on = str(_require(data, "fitted_on"))
    if fitted_on in FORBIDDEN_FIT_SPLITS:
        # rules.md R-37. The refusal is here, on the serving tier, because this is the last point at
        # which the mistake is still cheap: past it, every reported metric is contaminated and the
        # contamination is not visible in the numbers.
        raise CalibrationError(
            f"calibration was fitted on {fitted_on!r}. Platt scaling is fitted on "
            f"{REQUIRED_FIT_SPLIT!r} ONLY — the locked evaluation set stays untouched (rules.md R-37)."
        )
    if fitted_on != REQUIRED_FIT_SPLIT:
        raise CalibrationError(
            f"calibration fitted_on must be {REQUIRED_FIT_SPLIT!r}, got {fitted_on!r}"
        )

    method = str(_require(data, "method"))
    if method != METHOD_PLATT:
        # Isotonic regression, temperature scaling, and beta calibration are all defensible choices,
        # and none of them is `slope * x + intercept`. Applying this transform to their parameters
        # would produce a plausible curve that is not the fitted one.
        raise CalibrationError(
            f"unsupported calibration method: {method!r}. This service implements {METHOD_PLATT!r} "
            "(a two-parameter logistic) and nothing else."
        )

    slope = _finite_float(data, "slope")
    intercept = _finite_float(data, "intercept")
    if slope == 0.0:
        # A zero slope maps every raw score to the same probability. The service would come up
        # healthy, score every window, and return a constant — a detector that has stopped detecting
        # while still reporting.
        raise CalibrationError(
            "calibration slope is zero: every window would receive the same risk"
        )
    if slope < 0.0:
        raise CalibrationError(
            "calibration slope is negative, which inverts the detector: higher raw scores would map "
            "to lower spoof_risk. If the model's class orientation really is inverted, fix the "
            "orientation at export (playbook §7), not by flipping the calibration sign."
        )

    expected = data.get("contract_vector_raw_score")
    contract_vector_raw_score: float | None = None
    if expected is not None:
        contract_vector_raw_score = _finite_float(data, "contract_vector_raw_score")

    return Calibration(
        version=str(_require(data, "calibration_version")),
        sha256=sha256(raw_bytes).hexdigest(),
        status=status,
        method=method,
        slope=slope,
        intercept=intercept,
        model_version=str(_require(data, "model_version")),
        model_sha256=str(_require(data, "model_sha256")),
        fitted_on=fitted_on,
        contract_vector_raw_score=contract_vector_raw_score,
        source_path=str(path),
    )


def placeholder_calibration() -> Calibration:
    """The built-in mapping used when no calibration artifact exists yet.

    This is reachable ONLY in mock mode (see ``model.py`` and ``server.py``): in real mode a missing
    artifact is a hard refusal to start, because a real model whose output nobody has calibrated has
    no defensible mapping onto a risk.

    ``policy/calibration.json`` is Pair B's Phase-2 deliverable and does not exist in Phase 1, while
    ``docker compose up`` has to work on Day 1 (phases.md §2.1). Rather than have the Scorer refuse to
    start until an artifact lands — which would block Pair A's Gateway wiring and Pair C's integration
    harness on an ML deliverable two days out — the placeholder provides the identity transform
    ``sigmoid(raw)`` and labels itself as a placeholder in ``status``, ``version``, ``model_version``,
    and ``fitted_on``. Every one of those strings reaches the health response, the startup banner, and
    the audit row.

    ``model_sha256`` is the SHA-256 of the empty string, i.e. what you get by hashing nothing. That is
    both truthful (there is no model) and structurally distinguishable from a real hash, and it makes
    the Gateway's ``policy.calibration.model_sha256 != scorer_health.model_sha256`` check in
    ``gateway/app/main.py`` compare two well-defined values rather than one value and an empty string.
    """
    return Calibration(
        version="0.0.0-placeholder",
        sha256=sha256(b"placeholder-not-a-fitted-calibration").hexdigest(),
        status=PLACEHOLDER_STATUS,
        method=METHOD_PLATT,
        slope=1.0,
        intercept=0.0,
        model_version="mock-smoke-not-a-detector",
        model_sha256=sha256(b"").hexdigest(),
        fitted_on=REQUIRED_FIT_SPLIT,
        contract_vector_raw_score=None,
        source_path="<built-in placeholder>",
    )
