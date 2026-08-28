"""Adversarial Stress Test Suite for AI/ML Metrics, Codecs, and Robustness Pipeline.
Phase 4 Verification (SIH26104 / ToneDeaf).
"""

import copy
import hashlib
import json
from pathlib import Path
import numpy as np
import pytest

from audit.tests.test_dataset_schemas import MANIFEST_SCHEMA, grouping_key, validate
from evaluation.codecs import (
    apply_codec,
    apply_codec_chain,
    g711_alaw,
    get_available_codecs,
    gsm_8k_telephony,
    opus_compression,
)
from evaluation.metrics import (
    _normalize_labels,
    _normalize_scores,
    compute_brier_score,
    compute_confusion_matrix,
    compute_ece,
    compute_eer,
    compute_min_dcf,
    compute_min_tdcf,
    compute_pr_auc,
    compute_roc_auc,
    compute_roc_curve,
)
from evaluation.vc_generator import (
    generate_robustness_manifest_and_fixtures,
    generate_rvc_v2_artifacts,
    generate_sovits_artifacts,
    generate_synthetic_human_utterance,
)


class TestMetricsEngineStress:
    """Adversarial stress testing of evaluation/metrics.py."""

    @pytest.mark.parametrize("constant_val", [0.0, 0.5, 1.0, -10.0, 42.0])
    def test_identical_scores_zero_variance(self, constant_val):
        """Degenerate condition: All scores identical across bona fide and spoof."""
        labels = [0, 0, 0, 0, 1, 1, 1, 1]
        scores = [constant_val] * len(labels)
        fpr, tpr, thresholds = compute_roc_curve(labels, scores)
        assert len(fpr) == len(tpr) == len(thresholds)
        assert np.all(np.isfinite(fpr))
        assert np.all(np.isfinite(tpr))
        auc = compute_roc_auc(labels, scores)
        assert np.isclose(auc, 0.5)
        eer, thresh = compute_eer(labels, scores)
        assert 0.0 <= eer <= 1.0
        assert np.isfinite(thresh)
        min_dcf, best_thresh = compute_min_dcf(labels, scores, p_target=0.01)
        assert 0.0 <= min_dcf <= 1.05
        assert np.isfinite(best_thresh)

    def test_reversed_scores_anti_correlation(self):
        """Adversarial condition: Bona fide scores are high, spoof scores are low."""
        labels = [0, 0, 0, 1, 1, 1]
        scores = [0.95, 0.90, 0.85, 0.15, 0.10, 0.05]
        auc = compute_roc_auc(labels, scores)
        assert np.isclose(auc, 0.0)
        eer, thresh = compute_eer(labels, scores)
        assert np.isclose(eer, 1.0)
        min_dcf, _ = compute_min_dcf(labels, scores, p_target=0.01)
        assert min_dcf >= 1.0

    def test_nan_and_inf_scores_rejected(self):
        """Assert strict rejection of non-finite scores."""
        labels = [0, 1, 0, 1]
        with pytest.raises(ValueError, match="NaN or Inf"):
            compute_roc_curve(labels, [0.1, np.nan, 0.3, 0.4])
        with pytest.raises(ValueError, match="NaN or Inf"):
            compute_eer(labels, [0.1, np.inf, 0.3, 0.4])
        with pytest.raises(ValueError, match="NaN or Inf"):
            compute_min_dcf(labels, [0.1, -np.inf, 0.3, 0.4])
        with pytest.raises(ValueError, match="NaN or Inf"):
            compute_ece(labels, [0.1, np.nan, 0.3, 0.4])
        with pytest.raises(ValueError, match="NaN or Inf"):
            compute_brier_score(labels, [0.1, np.inf, 0.3, 0.4])

    def test_label_normalization_heterogeneous(self):
        """Verify robust label normalization across types and string formats."""
        labels = [False, True, 0.0, 1.0, 0.49, 0.51]
        norm = _normalize_labels(labels)
        assert np.array_equal(norm, [0, 1, 0, 1, 0, 1])
        bonafide_aliases = ["0", "false", "bonafide", "bona_fide", "genuine", "real", "human"]
        spoof_aliases = ["1", "true", "spoof", "fake", "synthetic", "converted", "attack"]
        assert np.all(_normalize_labels(bonafide_aliases) == 0)
        assert np.all(_normalize_labels(spoof_aliases) == 1)
        with pytest.raises(ValueError, match="Unrecognized label value"):
            _normalize_labels(["invalid_category"])

    def test_empty_and_mismatched_inputs(self):
        """Boundary edge cases: empty arrays and length mismatches."""
        with pytest.raises(ValueError):
            compute_roc_curve([], [])
        with pytest.raises(ValueError):
            compute_roc_auc([], [])
        with pytest.raises(ValueError):
            compute_roc_curve([0, 1], [0.5])
        with pytest.raises(ValueError, match="both bona fide"):
            compute_roc_curve([0, 0, 0], [0.1, 0.2, 0.3])
        with pytest.raises(ValueError, match="both bona fide"):
            compute_roc_curve([1, 1, 1], [0.1, 0.2, 0.3])

    @pytest.mark.parametrize("p_target", [1e-5, 1e-4, 1e-3, 0.01, 0.05, 0.1, 0.5, 0.9, 0.99])
    @pytest.mark.parametrize("c_miss,c_fa", [(1.0, 1.0), (1.0, 10.0), (10.0, 1.0), (0.1, 5.0)])
    def test_min_dcf_extreme_parameter_sweep(self, p_target, c_miss, c_fa):
        """Stress-test minDCF across extreme priors and cost weighting."""
        np.random.seed(1234)
        labels = np.r_[np.zeros(200), np.ones(200)]
        scores = np.r_[np.random.normal(0.2, 0.15, 200), np.random.normal(0.8, 0.15, 200)]
        min_dcf, best_thresh = compute_min_dcf(labels, scores, p_target=p_target, c_miss=c_miss, c_fa=c_fa)
        assert np.isfinite(min_dcf)
        assert 0.0 <= min_dcf <= 1.05
        assert np.isfinite(best_thresh)

    @pytest.mark.parametrize("num_bins", [2, 3, 5, 10, 20, 50, 100, 200])
    def test_ece_reliability_and_bin_sweep(self, num_bins):
        """Verify ECE stability across fine and coarse bin partitions."""
        np.random.seed(42)
        n = 500
        risks = np.random.uniform(0.0, 1.0, n)
        labels = (np.random.uniform(0.0, 1.0, n) < risks).astype(int)
        ece = compute_ece(labels, risks, num_bins=num_bins)
        assert np.isfinite(ece)
        assert 0.0 <= ece <= 1.0
        edge_labels = [0, 1]
        edge_risks = [0.0, 1.0]
        ece_perfect = compute_ece(edge_labels, edge_risks, num_bins=num_bins)
        assert np.isclose(ece_perfect, 0.0)

    def test_ece_out_of_bounds_rejection(self):
        """ECE requires calibrated probabilities in [0, 1]."""
        labels = [0, 1, 0, 1]
        with pytest.raises(ValueError, match="Risks must be bounded"):
            compute_ece(labels, [-0.01, 0.5, 0.8, 0.9])
        with pytest.raises(ValueError, match="Risks must be bounded"):
            compute_ece(labels, [0.1, 0.5, 1.05, 0.9])

    def test_confusion_matrix_extremes(self):
        """Test confusion matrix at out-of-range thresholds."""
        labels = [0, 0, 1, 1]
        scores = [0.2, 0.3, 0.7, 0.8]
        cm_high = compute_confusion_matrix(labels, scores, threshold=10.0)
        assert cm_high["tp"] == 0
        assert cm_high["fp"] == 0
        assert cm_high["tn"] == 2
        assert cm_high["fn"] == 2
        assert cm_high["precision"] == 0.0
        assert cm_high["recall"] == 0.0
        assert cm_high["accuracy"] == 0.5
        cm_low = compute_confusion_matrix(labels, scores, threshold=-10.0)
        assert cm_low["tp"] == 2
        assert cm_low["fp"] == 2
        assert cm_low["tn"] == 0
        assert cm_low["fn"] == 0
        assert cm_low["precision"] == 0.5
        assert cm_low["recall"] == 1.0
        assert cm_low["accuracy"] == 0.5


class TestCodecsPipelineStress:
    """Adversarial stress testing of evaluation/codecs.py."""

    ALL_CODECS = ["clean_16k", "gsm_8k", "g711_alaw", "opus_24k", "opus_12k", "opus_8k", "opus_24k_then_g711"]

    @pytest.mark.parametrize("codec_name", ALL_CODECS)
    def test_silence_input(self, codec_name):
        """Silence (all zeros) must not produce NaNs, Infs, or energy explosions."""
        silence = np.zeros(16000, dtype=np.float32)
        out = apply_codec(silence, codec_name)
        assert len(out) == len(silence)
        assert out.dtype == np.float32
        assert np.all(np.isfinite(out))
        rms = np.sqrt(np.mean(out ** 2))
        assert rms < 1e-3

    @pytest.mark.parametrize("codec_name", ALL_CODECS)
    def test_dc_offset_signal(self, codec_name):
        """Constant DC offset signal."""
        dc_signal = np.full(8000, 0.75, dtype=np.float32)
        out = apply_codec(dc_signal, codec_name)
        assert len(out) == len(dc_signal)
        assert out.dtype == np.float32
        assert np.all(np.isfinite(out))
        assert np.max(np.abs(out)) <= 1.0

    @pytest.mark.parametrize("codec_name", ALL_CODECS)
    def test_clipping_input(self, codec_name):
        """Input signals with amplitude exceeding [-1.0, 1.0]."""
        clipped = np.array([2.5, -3.0, 1.8, -4.2, 5.0] * 500, dtype=np.float32)
        out = apply_codec(clipped, codec_name)
        assert len(out) == len(clipped)
        assert out.dtype == np.float32
        assert np.all(np.isfinite(out))
        assert np.max(np.abs(out)) <= 1.0

    @pytest.mark.parametrize("length", [1, 5, 10, 20, 39, 40, 80, 159, 160, 319, 320])
    @pytest.mark.parametrize("codec_name", ["gsm_8k", "g711_alaw", "opus_24k"])
    def test_short_frame_boundaries(self, length, codec_name):
        """Sub-frame and boundary sample lengths."""
        audio = np.sin(np.linspace(0, 4 * np.pi, length, dtype=np.float32))
        out = apply_codec(audio, codec_name)
        assert len(out) == length
        assert out.dtype == np.float32
        assert np.all(np.isfinite(out))

    @pytest.mark.parametrize("sr", [4000, 8000, 11025, 22050, 44100, 48000, 96000])
    def test_extreme_sample_rates(self, sr):
        """Test codec ingestion at non-16khz sample rates."""
        duration = 0.5
        n_samples = int(sr * duration)
        t = np.linspace(0, duration, n_samples, endpoint=False, dtype=np.float32)
        signal = 0.8 * np.sin(2 * np.pi * 440.0 * t)
        for codec in ["gsm_8k", "g711_alaw", "opus_24k"]:
            out = apply_codec(signal, codec, sr=sr)
            assert len(out) == n_samples
            assert np.all(np.isfinite(out))
            assert np.max(np.abs(out)) <= 1.0

    def test_empty_audio_handling(self):
        """Empty array should return empty array safely."""
        empty = np.array([], dtype=np.float32)
        for codec in self.ALL_CODECS:
            out = apply_codec(empty, codec)
            assert len(out) == 0
            assert out.dtype == np.float32

    def test_2d_stereo_conversion(self):
        """2D multi-channel audio should collapse to 1D mono safely."""
        stereo = np.zeros((16000, 2), dtype=np.float32)
        stereo[:, 0] = 0.5
        stereo[:, 1] = -0.5
        out = apply_codec(stereo, "opus_24k")
        assert out.ndim == 1
        assert len(out) == 16000

    def test_multi_hop_chain_stability(self):
        """Multi-hop cascading codec degradation."""
        sr = 16000
        t = np.linspace(0, 2.0, int(sr * 2.0), endpoint=False, dtype=np.float32)
        signal = 0.7 * np.sin(2 * np.pi * 300.0 * t) + 0.3 * np.sin(2 * np.pi * 1200.0 * t)
        chain = ["opus_24k", "gsm_8k", "g711_alaw", "opus_12k"]
        out = apply_codec_chain(signal, chain)
        assert len(out) == len(signal)
        assert np.all(np.isfinite(out))
        assert np.max(np.abs(out)) <= 1.0
        rms = np.sqrt(np.mean(out ** 2))
        assert 0.01 < rms < 1.0


import sys

class TestVCGeneratorAndManifestIntegrity:
    """Adversarial stress testing of VC generator and manifest consistency."""

    def test_vc_vectors_length_and_dtype(self):
        """Verify all generated audio vectors produce exact 16kHz float32 audio."""
        for spk_id in [1, 2, 3, 5, 8]:
            human = generate_synthetic_human_utterance(speaker_id=spk_id, seed=100 + spk_id, duration_sec=2.56)
            assert len(human) == 40960 and human.dtype == np.float32
            assert np.all(np.isfinite(human)) and np.max(np.abs(human)) <= 1.0

            rvc = generate_rvc_v2_artifacts(human, seed=200 + spk_id)
            assert len(rvc) == 40960 and rvc.dtype == np.float32
            assert np.all(np.isfinite(rvc)) and np.max(np.abs(rvc)) <= 1.0

            sovits = generate_sovits_artifacts(human, seed=300 + spk_id)
            assert len(sovits) == 40960 and sovits.dtype == np.float32
            assert np.all(np.isfinite(sovits)) and np.max(np.abs(sovits)) <= 1.0

    def test_manifest_schema_zero_violations(self):
        """Verify datasets/manifest/vc_robustness.manifest.json strictly complies with manifest.schema.json."""
        manifest_path = Path("datasets/manifest/vc_robustness.manifest.json")
        doc = generate_robustness_manifest_and_fixtures(str(manifest_path), num_base_speakers=8)
        errors = validate(doc, MANIFEST_SCHEMA)
        assert errors == [], f"Schema validation errors: {errors}"
        assert doc["schema_version"] == "1.0.0"
        assert doc["manifest_id"] == "vc-robustness-evaluation-v1"
        records = doc["records"]
        # D-09 compliant structure: rvc only in eval_generator_heldout (spk 7-8),
        # so-vits-svc only in dev_calibration + codec-heldout (spk 1-6).
        # Bonafide (none) across all 8 speakers × 5 codecs = 40.
        # so-vits-svc: 6 speakers × 5 codecs = 30.
        # rvc: 2 speakers × 5 codecs = 10.
        # Total: 40 + 30 + 10 = 80.
        assert len(records) == 80, f"Expected 80 D-09-compliant records, got {len(records)}"
        splits_by_key = {}
        for r in records:
            expected_key = grouping_key(r)
            actual_key = r["grouping"]["grouping_key_sha256"]
            assert actual_key == expected_key, f"Mismatch in record {r['sample_id']}"
            splits_by_key.setdefault(actual_key, set()).add(r["split"])
            assert r["duration_ms"] == 2560
            assert r["sample_rate_hz"] in (8000, 16000)
            assert len(r["sha256_audio"]) == 64
        straddling = {k: v for k, v in splits_by_key.items() if len(v) > 1}
        assert straddling == {}, f"Grouping keys straddle multiple splits: {straddling}"

    def test_manifest_validation_via_script(self):
        """Run scripts/validate_manifest.py on vc_robustness.manifest.json."""
        import subprocess
        res = subprocess.run(
            [
                sys.executable,
                "scripts/validate_manifest.py",
                "datasets/manifest/vc_robustness.manifest.json",
            ],
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, f"validate_manifest.py failed: {res.stdout}\n{res.stderr}"
