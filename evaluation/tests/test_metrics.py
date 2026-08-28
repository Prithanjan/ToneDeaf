"""Unit tests for Anti-Spoofing Metrics Engine (evaluation/metrics.py)."""

import numpy as np
import pytest

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


class TestLabelAndScoreNormalization:
    def test_normalize_labels_various_types(self):
        # Integer / Bool
        assert np.array_equal(_normalize_labels([0, 1, 0, 1]), np.array([0, 1, 0, 1]))
        assert np.array_equal(_normalize_labels([False, True]), np.array([0, 1]))
        # Float
        assert np.array_equal(_normalize_labels([0.1, 0.9]), np.array([0, 1]))
        # Strings
        labels_str = ["bonafide", "spoof", "genuine", "attack", "real", "fake"]
        assert np.array_equal(_normalize_labels(labels_str), np.array([0, 1, 0, 1, 0, 1]))

    def test_normalize_labels_invalid(self):
        with pytest.raises(ValueError, match="Unrecognized label value"):
            _normalize_labels(["unknown_category"])

    def test_normalize_scores_nan_inf(self):
        with pytest.raises(ValueError, match="NaN or Inf"):
            _normalize_scores([0.5, np.nan, 0.9])
        with pytest.raises(ValueError, match="NaN or Inf"):
            _normalize_scores([0.5, np.inf, 0.9])


class TestROCandAUC:
    def test_perfect_separation(self):
        labels = [0, 0, 0, 1, 1, 1]
        scores = [0.1, 0.2, 0.3, 0.7, 0.8, 0.9]
        auc = compute_roc_auc(labels, scores)
        assert np.isclose(auc, 1.0)
        pr_auc = compute_pr_auc(labels, scores)
        assert np.isclose(pr_auc, 1.0)

    def test_inverted_separation(self):
        labels = [0, 0, 0, 1, 1, 1]
        scores = [0.9, 0.8, 0.7, 0.3, 0.2, 0.1]
        auc = compute_roc_auc(labels, scores)
        assert np.isclose(auc, 0.0)

    def test_roc_curve_monotonicity(self):
        labels = [0, 1, 0, 1, 0, 1, 0, 1]
        scores = [0.1, 0.3, 0.4, 0.6, 0.5, 0.8, 0.2, 0.9]
        fpr, tpr, thresholds = compute_roc_curve(labels, scores)
        assert len(fpr) == len(tpr) == len(thresholds)
        assert fpr[0] == 0.0 and tpr[0] == 0.0
        assert fpr[-1] == 1.0 and tpr[-1] == 1.0
        # FPR and TPR must be non-decreasing
        assert np.all(np.diff(fpr) >= -1e-12)
        assert np.all(np.diff(tpr) >= -1e-12)

    def test_roc_validation_errors(self):
        with pytest.raises(ValueError, match="Length mismatch"):
            compute_roc_curve([0, 1], [0.5])
        with pytest.raises(ValueError, match="both bona fide"):
            compute_roc_curve([0, 0, 0], [0.1, 0.2, 0.3])
        with pytest.raises(ValueError, match="both bona fide"):
            compute_roc_curve([1, 1, 1], [0.7, 0.8, 0.9])


class TestEER:
    def test_eer_perfect_separation(self):
        labels = [0, 0, 0, 1, 1, 1]
        scores = [0.1, 0.2, 0.3, 0.7, 0.8, 0.9]
        eer, threshold = compute_eer(labels, scores)
        assert np.isclose(eer, 0.0)
        assert 0.3 <= threshold <= 0.7

    def test_eer_symmetric_overlap(self):
        # 100 genuine around 0.3, 100 spoof around 0.7
        np.random.seed(42)
        bonafide = np.random.normal(0.3, 0.1, 500)
        spoof = np.random.normal(0.7, 0.1, 500)
        labels = np.r_[np.zeros(500), np.ones(500)]
        scores = np.r_[bonafide, spoof]
        eer, threshold = compute_eer(labels, scores)
        assert 0.0 < eer < 0.1  # Normal distributions with 4 sigma separation have small EER
        assert np.isclose(threshold, 0.5, atol=0.1)

    def test_eer_random_guessing(self):
        np.random.seed(42)
        labels = np.r_[np.zeros(1000), np.ones(1000)]
        scores = np.random.uniform(0.0, 1.0, 2000)
        eer, threshold = compute_eer(labels, scores)
        assert np.isclose(eer, 0.5, atol=0.08)


class TestMinDCF:
    def test_min_dcf_perfect_separation(self):
        labels = [0, 0, 0, 1, 1, 1]
        scores = [0.1, 0.2, 0.3, 0.7, 0.8, 0.9]
        min_dcf, thresh = compute_min_dcf(labels, scores, p_target=0.01)
        assert np.isclose(min_dcf, 0.0)
        assert 0.3 <= thresh <= 0.7

    def test_min_dcf_bounded(self):
        np.random.seed(42)
        labels = np.r_[np.zeros(200), np.ones(200)]
        scores = np.random.uniform(0.0, 1.0, 400)
        min_dcf, thresh = compute_min_dcf(labels, scores, p_target=0.01)
        assert 0.0 <= min_dcf <= 1.05


class TestMinTDCF:
    def test_min_tdcf_standalone(self):
        labels = [0, 0, 0, 0, 1, 1, 1, 1]
        cm_scores = [0.1, 0.2, 0.15, 0.25, 0.85, 0.9, 0.75, 0.95]
        min_tdcf, thresh = compute_min_tdcf(None, cm_scores, labels)
        assert np.isclose(min_tdcf, 0.0)

    def test_min_tdcf_with_asv_scores(self):
        labels = [0, 0, 1, 1]
        cm_scores = [0.1, 0.2, 0.8, 0.9]
        asv_scores = [0.9, 0.85, 0.3, 0.2]
        min_tdcf, thresh = compute_min_tdcf(asv_scores, cm_scores, labels)
        assert np.isclose(min_tdcf, 0.0)


class TestECEandBrier:
    def test_ece_perfect_calibration(self):
        # Samples with probability 0.1 all 0, probability 0.9 all 1
        labels = [0] * 90 + [1] * 10 + [0] * 10 + [1] * 90
        risks = [0.1] * 100 + [0.9] * 100
        ece = compute_ece(labels, risks, num_bins=10)
        assert np.isclose(ece, 0.0, atol=1e-3)

    def test_ece_poor_calibration(self):
        labels = [0] * 100  # all genuine
        risks = [0.9] * 100  # model confidently claims 90% spoof
        ece = compute_ece(labels, risks, num_bins=10)
        assert np.isclose(ece, 0.9, atol=1e-3)

    def test_brier_score_extremes(self):
        # Perfect
        assert np.isclose(compute_brier_score([0, 1], [0.0, 1.0]), 0.0)
        # Completely inverted
        assert np.isclose(compute_brier_score([0, 1], [1.0, 0.0]), 1.0)
        # 0.5 on everything
        assert np.isclose(compute_brier_score([0, 1], [0.5, 0.5]), 0.25)


class TestConfusionMatrix:
    def test_confusion_matrix_metrics(self):
        labels = [0, 0, 1, 1]
        scores = [0.2, 0.6, 0.4, 0.8]
        # Threshold = 0.5:
        # Pred: [0, 1, 0, 1]
        # TP = 1 (item 4), FP = 1 (item 2), TN = 1 (item 1), FN = 1 (item 3)
        cm = compute_confusion_matrix(labels, scores, threshold=0.5)
        assert cm["tp"] == 1
        assert cm["fp"] == 1
        assert cm["tn"] == 1
        assert cm["fn"] == 1
        assert np.isclose(cm["precision"], 0.5)
        assert np.isclose(cm["recall"], 0.5)
        assert np.isclose(cm["f1"], 0.5)
        assert np.isclose(cm["accuracy"], 0.5)
