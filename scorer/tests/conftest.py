"""Shared fixtures. Same conventions as ``gateway/tests/conftest.py``.

``scorer/`` is put on ``sys.path`` here rather than in a ``scorer/conftest.py`` because ``tests/`` has no
``__init__.py``: pytest inserts ``scorer/tests`` on the path, not ``scorer``, so ``import app`` would
fail. ``conftest.py`` is imported before any test module, which makes this the earliest hook available
without adding a package marker that would change collection semantics.

NO PLAUSIBLE-LOOKING SECRETS ANYWHERE IN THIS DIRECTORY. Every literal below is either a version
string, a shape, a hash of something public, or a value whose name says it is an example. A test fixture
that looks like a credential is a credential as far as a secret scanner, a screenshot, or a future
copy-paste is concerned (rules.md R-34) — and ``test_packaging.py`` asserts this as a property of the
files rather than trusting it as a habit.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The three imports below MUST follow the sys.path insert above; moving them into the import block at
# the top of the file makes every test in this directory fail with ModuleNotFoundError: app.
from app.calibration import (
    FITTED_STATUS,
    PLACEHOLDER_STATUS,
    REQUIRED_FIT_SPLIT,
    Calibration,
    load_calibration,
)
from app.config import ScorerSettings, load_settings
from app.contract import WINDOW_BYTES, WINDOW_SAMPLES

REPO_ROOT = Path(__file__).resolve().parents[2]
SCORER_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_VECTOR_PATH = REPO_ROOT / "ml" / "fixtures" / "contract_vector_v1.npy"

#: SHA-256 of a model file that does not exist. Deliberately the hash of a descriptive byte string, not
#: 64 random hex characters: a reader must be able to tell at a glance that it is a test value.
EXAMPLE_MODEL_SHA256 = sha256(b"example-onnx-artifact-for-tests").hexdigest()


@pytest.fixture
def base_env() -> dict[str, str]:
    """A minimal VALID environment. Tests mutate a copy to make exactly one thing wrong.

    Built as a dict and passed to ``load_settings(env=...)`` rather than set on ``os.environ``, so a
    test that raises partway through cannot leak a variable into every later test in the process.
    """
    return {
        "DEPLOYMENT_PROFILE": "local-cpu",
        "EXECUTION_PROVIDER": "CPUExecutionProvider",
        "DETECTOR_MODE": "MOCK_SMOKE_MODE_NOT_A_DETECTOR",
        "ARTIFACT_STATE": "research_only",
        "MODEL_PATH": "/models/does-not-exist.onnx",
        "CALIBRATION_PATH": "/policy/does-not-exist.json",
        "CONTRACT_VECTOR_PATH": str(CONTRACT_VECTOR_PATH),
        "LOG_LEVEL": "INFO",
        "GIT_COMMIT": "0000000",
    }


@pytest.fixture
def mock_settings(base_env: dict[str, str]) -> ScorerSettings:
    return load_settings(env=base_env)


@pytest.fixture
def fitted_calibration_document() -> dict[str, object]:
    """A well-formed ``calibration.json``, shaped like Pair B's Phase-2 deliverable."""
    return {
        "calibration_version": "1.0.0-test",
        "status": FITTED_STATUS,
        "method": "platt",
        "slope": 2.5,
        "intercept": -1.25,
        "model_version": "aasist-test-0",
        "model_sha256": EXAMPLE_MODEL_SHA256,
        "fitted_on": REQUIRED_FIT_SPLIT,
    }


@pytest.fixture
def write_calibration(tmp_path: Path) -> Callable[[dict[str, object]], Path]:
    """Write a calibration document to a temp file and return the path."""

    def _write(document: dict[str, object], name: str = "calibration.json") -> Path:
        path = tmp_path / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    return _write


@pytest.fixture
def fitted_calibration(
    fitted_calibration_document: dict[str, object],
    write_calibration: Callable[..., Path],
) -> Calibration:
    return load_calibration(write_calibration(fitted_calibration_document))


@pytest.fixture(scope="session")
def contract_vector() -> np.ndarray:
    """The committed ``(1, 40960)`` float32 parity fixture (frame_contract.md §6).

    ``allow_pickle=False``: a ``.npy`` file can carry a pickled object and unpickling is arbitrary code
    execution. This is the same flag ``server.py`` uses, so the test path and the serving path agree
    about what this file is allowed to be.
    """
    if not CONTRACT_VECTOR_PATH.is_file():
        pytest.skip("contract_vector_v1.npy not generated; run ml/fixtures/make_contract_vector.py")
    return np.load(CONTRACT_VECTOR_PATH, allow_pickle=False)


@pytest.fixture
def silent_window() -> np.ndarray:
    """All-zero window: the LOW_ENERGY case, and the one that must be INELIGIBLE, never low-risk."""
    return np.zeros((1, WINDOW_SAMPLES), dtype=np.float32)


@pytest.fixture
def valid_pcm() -> bytes:
    """81,920 bytes of int16 PCM with enough energy to be an eligible window."""
    samples = np.arange(WINDOW_SAMPLES, dtype=np.int64)
    # A deterministic sawtooth in int16 range, mean-centred so it does not trip DC_OFFSET.
    values = ((samples * 997) % 16_001) - 8_000
    payload = values.astype("<i2").tobytes()
    assert len(payload) == WINDOW_BYTES
    return payload


@pytest.fixture
def placeholder_status() -> str:
    return PLACEHOLDER_STATUS
