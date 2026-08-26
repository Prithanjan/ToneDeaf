"""Policy bundle and calibration artifact loading.

Loads ``policy/policy.yaml`` and ``policy/calibration.json``, hashes both, and enforces the
eligibility rules that keep a placeholder threshold from being presented as a calibrated decision.

The two gates worth naming:

* **A score is not calibrated until a calibration artifact says so** (rules.md R-11). While
  ``calibration.json`` carries ``status: placeholder-not-policy-eligible``, the bundle loads and the
  demo runs, but ``artifact_state`` can never reach ``policy_eligible`` — so no probability language
  is permitted in UI, logs, or docs, and CI blocks the release.
* **``approve`` / ``deny`` cannot be configured into existence** (rules.md R-07). The action map is
  parsed into the closed :class:`~app.policy.engine.Action` enum, so a bundle containing one fails
  to load rather than quietly introducing an authorization outcome.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Final

import yaml

from app.policy.engine import Action, PolicyThresholds, PurposeActionMap, RiskState

REQUIRED_STATES: Final[frozenset[RiskState]] = frozenset(RiskState)
PLACEHOLDER_STATUS: Final[str] = "placeholder-not-policy-eligible"

VALID_ARTIFACT_STATES: Final[tuple[str, ...]] = (
    "research_only",
    "demo_eligible",
    "policy_eligible",
)


class PolicyLoadError(ValueError):
    """The policy bundle or calibration artifact is invalid. Refuse to start."""


@dataclass(frozen=True, slots=True)
class CalibrationArtifact:
    """Identity and eligibility of the calibration mapping.

    The Gateway does not apply the Platt transform — the Scorer does. The Gateway loads this to
    verify that the artifacts it is *about* to make decisions from are the ones it thinks they are,
    and to stamp their hashes into every audit row.
    """

    version: str
    sha256: str
    status: str
    model_version: str
    model_sha256: str

    @property
    def is_policy_eligible(self) -> bool:
        return self.status != PLACEHOLDER_STATUS


@dataclass(frozen=True, slots=True)
class PolicyBundle:
    """A loaded, validated policy bundle plus its hash."""

    version: str
    sha256: str
    thresholds: PolicyThresholds
    purpose_actions: PurposeActionMap
    threshold_derivation: str
    calibration: CalibrationArtifact

    @property
    def artifact_state(self) -> str:
        """The strongest state these artifacts support.

        Derived, never declared: a bundle cannot assert ``policy_eligible`` for itself. The
        threshold derivation and the calibration status both have to earn it.
        """
        if self.threshold_derivation == "placeholder" or not self.calibration.is_policy_eligible:
            return "demo_eligible"
        return "policy_eligible"

    @property
    def allows_probability_language(self) -> bool:
        """Whether UI and logs may describe ``spoof_risk`` as a probability (rules.md R-11)."""
        return self.calibration.is_policy_eligible


def _sha256_file(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _parse_action(value: Any, *, where: str) -> Action:
    try:
        return Action(str(value))
    except ValueError as exc:
        # The specific failure this catches: someone adds `high: deny` to the bundle to make the
        # demo more dramatic. There is no enum member, so the bundle does not load (rules.md R-07).
        raise PolicyLoadError(
            f"{where}: {value!r} is not a valid action. The vocabulary is closed: "
            f"{[a.value for a in Action]}. 'approve' and 'deny' do not exist in this system."
        ) from exc


def _parse_purpose_actions(raw: Any) -> PurposeActionMap:
    if not isinstance(raw, dict) or not raw:
        raise PolicyLoadError("purpose_actions must be a non-empty mapping")

    parsed: PurposeActionMap = {}
    for purpose, states in raw.items():
        if not isinstance(states, dict):
            raise PolicyLoadError(
                f"purpose_actions.{purpose} must be a mapping of risk_state to action"
            )

        by_state: dict[RiskState, Action] = {}
        for state_name, action in states.items():
            try:
                state = RiskState(str(state_name))
            except ValueError as exc:
                raise PolicyLoadError(
                    f"purpose_actions.{purpose}: unknown risk_state {state_name!r}"
                ) from exc
            by_state[state] = _parse_action(action, where=f"purpose_actions.{purpose}.{state_name}")

        missing = REQUIRED_STATES - by_state.keys()
        if missing:
            # An unmapped state would mean a KeyError at decision time, i.e. a crash during the
            # demo at exactly the moment risk was detected.
            raise PolicyLoadError(
                f"purpose_actions.{purpose} is missing states: {sorted(s.value for s in missing)}"
            )
        parsed[str(purpose)] = by_state

    return parsed


def load_calibration(path: Path) -> CalibrationArtifact:
    """Load and hash ``calibration.json``."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PolicyLoadError(f"cannot read calibration artifact: {path}") from exc

    for key in ("calibration_version", "status", "model_version", "model_sha256"):
        if key not in data:
            raise PolicyLoadError(f"calibration artifact missing required key: {key}")

    return CalibrationArtifact(
        version=str(data["calibration_version"]),
        sha256=_sha256_file(path),
        status=str(data["status"]),
        model_version=str(data["model_version"]),
        model_sha256=str(data["model_sha256"]),
    )


def load_policy(policy_path: Path, calibration_path: Path) -> PolicyBundle:
    """Load, validate, and hash the policy bundle. Raises rather than falling back to defaults.

    There is deliberately no default threshold in code. A missing or malformed bundle means the
    Gateway does not know what decision it is supposed to make, and inventing one silently is how a
    demo ends up enforcing a number nobody chose.
    """
    try:
        raw = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PolicyLoadError(f"cannot read policy bundle: {policy_path}") from exc

    if not isinstance(raw, dict):
        raise PolicyLoadError("policy bundle must be a mapping")

    version = raw.get("policy_version")
    if not version:
        raise PolicyLoadError("policy bundle missing policy_version")

    thresholds_raw = raw.get("thresholds")
    if not isinstance(thresholds_raw, dict):
        raise PolicyLoadError("policy bundle missing thresholds")

    evidence = raw.get("evidence")
    if not isinstance(evidence, dict):
        raise PolicyLoadError("policy bundle missing evidence")

    try:
        thresholds = PolicyThresholds(
            high_window_risk=float(thresholds_raw["high_window_risk"]),
            evidence_k=int(evidence["k"]),
            evidence_n=int(evidence["n"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyLoadError(f"invalid thresholds/evidence: {exc}") from exc

    derivation = str(thresholds_raw.get("derivation", "placeholder"))

    calibration = load_calibration(calibration_path)
    declared_model = raw.get("model_version")
    if declared_model is None or str(declared_model).strip() == "":
        raise PolicyLoadError("policy bundle missing model_version")
    if str(declared_model) != calibration.model_version:
        # Catches the pairing mistake: a policy bundle tuned for one model, deployed with another.
        # Silent here means every threshold is being applied to a different score distribution.
        raise PolicyLoadError(
            "policy bundle model_version does not match the calibration artifact "
            f"({declared_model!r} vs {calibration.model_version!r})"
        )

    return PolicyBundle(
        version=str(version),
        sha256=_sha256_file(policy_path),
        thresholds=thresholds,
        purpose_actions=_parse_purpose_actions(raw.get("purpose_actions")),
        threshold_derivation=derivation,
        calibration=calibration,
    )
