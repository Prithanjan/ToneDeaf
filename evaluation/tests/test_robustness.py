"""Unit and integration tests for Robustness Evaluation Suite & VC Generator."""

import json
from pathlib import Path
import numpy as np
import pytest

from audit.tests.test_dataset_schemas import MANIFEST_SCHEMA, grouping_key, validate
from evaluation.codecs import apply_codec
from evaluation.vc_generator import (
    generate_robustness_manifest_and_fixtures,
    generate_rvc_v2_artifacts,
    generate_sovits_artifacts,
    generate_synthetic_human_utterance,
)


@pytest.fixture(scope="module")
def manifest_data():
    manifest_path = Path("datasets/manifest/vc_robustness.manifest.json")
    doc = generate_robustness_manifest_and_fixtures(str(manifest_path))
    return doc


class TestVCRobustnessManifest:
    def test_manifest_schema_validation(self, manifest_data):
        errors = validate(manifest_data, MANIFEST_SCHEMA)
        assert errors == [], "vc_robustness.manifest.json failed schema validation: " + "; ".join(errors)

    def test_grouping_key_reproducibility(self, manifest_data):
        for record in manifest_data["records"]:
            expected = grouping_key(record)
            actual = record["grouping"]["grouping_key_sha256"]
            assert actual == expected, f"Record {record['sample_id']} grouping key mismatch"

    def test_split_disjointness_invariant(self, manifest_data):
        splits_by_key: dict[str, set[str]] = {}
        for record in manifest_data["records"]:
            splits_by_key.setdefault(
                record["grouping"]["grouping_key_sha256"], set()
            ).add(record["split"])
        straddling = {k: sorted(v) for k, v in splits_by_key.items() if len(v) > 1}
        assert straddling == {}, f"Grouping keys straddle splits: {straddling}"

    def test_audio_digests_unique(self, manifest_data):
        digests = [r["sha256_audio"] for r in manifest_data["records"]]
        assert len(digests) == len(set(digests)), "Duplicate audio hashes found in manifest"


class TestVCGenerators:
    def test_human_carrier_generation(self):
        human = generate_synthetic_human_utterance(speaker_id=1, seed=42, duration_sec=2.56)
        assert len(human) == 40960
        assert human.dtype == np.float32
        assert np.max(np.abs(human)) <= 1.0

    def test_rvc_v2_artifacts(self):
        human = generate_synthetic_human_utterance(speaker_id=2, seed=43, duration_sec=2.56)
        rvc = generate_rvc_v2_artifacts(human, seed=101, pitch_shift_semitones=2.0)
        assert len(rvc) == len(human)
        assert rvc.dtype == np.float32
        diff = np.mean(np.abs(rvc - human))
        assert diff > 0.02

    def test_sovits_artifacts(self):
        human = generate_synthetic_human_utterance(speaker_id=3, seed=44, duration_sec=2.56)
        sovits = generate_sovits_artifacts(human, seed=202, formant_warp=1.12)
        assert len(sovits) == len(human)
        assert sovits.dtype == np.float32
        diff = np.mean(np.abs(sovits - human))
        assert diff > 0.02
