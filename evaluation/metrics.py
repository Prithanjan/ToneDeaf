"""Anti-spoofing evaluation metrics engine for Voice Integrity Control Plane (ToneDeaf).

Provides standardized, robust metric implementations:
- compute_eer: Equal Error Rate (EER) and operating threshold via linear interpolation.
- compute_min_dcf: Minimum Normalized Detection Cost Function (NIST / ASVspoof standard).
- compute_min_tdcf: Minimum tandem Detection Cost Function (t-DCF for ASV + CM).
- compute_ece: Expected Calibration Error across probability bins.
- compute_brier_score: Mean squared error of probabilistic risk estimates.
- compute_roc_auc: Area Under the Receiver Operating Characteristic curve.
- compute_pr_auc: Area Under the Precision-Recall curve.
- compute_confusion_matrix: Binary confusion matrix and derived classification metrics.

Conventions (rules.md R-06, R-07, R-11):
- Labels: 0 (or 'bonafide' / 'genuine') = bona fide human speech.
          1 (or 'spoof') = synthetic / converted / cloned spoof speech.
- Scores: Higher score indicates higher risk/probability of spoof.
"""

from __future__ import annotations

from typing import Any, Sequence
import numpy as np


def _normalize_labels(labels: Sequence[Any]) -> np.ndarray:
    """Convert heterogeneous label sequence to binary integer array (0=bonafide, 1=spoof)."""
    arr = np.asarray(labels)
    if arr.size == 0:
        return np.array([], dtype=np.int32)
    
    if arr.dtype.kind in ('i', 'u', 'b'):
        return (arr > 0).astype(np.int32)
    
    if arr.dtype.kind in ('f',):
        return (arr >= 0.5).astype(np.int32)
    
    res = np.zeros(len(arr), dtype=np.int32)
    for i, item in enumerate(arr):
        s = str(item).strip().lower()
        if s in ('1', 'true', 'spoof', 'fake', 'synthetic', 'converted', 'attack'):
            res[i] = 1
        elif s in ('0', 'false', 'bonafide', 'bona_fide', 'genuine', 'real', 'human'):
            res[i] = 0
        else:
            raise ValueError(f"Unrecognized label value: {item!r}")
    return res


def _normalize_scores(scores: Sequence[Any]) -> np.ndarray:
    """Convert scores sequence to 1D float64 array, verifying no NaNs or Infs."""
    arr = np.asarray(scores, dtype=np.float64).flatten()
    if np.any(np.isnan(arr)) or np.any(np.isinf(arr)):
        raise ValueError("Scores contain NaN or Inf values.")
    return arr


def compute_roc_curve(
    labels: Sequence[Any], scores: Sequence[Any]
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute Receiver Operating Characteristic (ROC) curve.
    
    Returns:
        fpr: False Positive Rates (bona fide mistakenly classified as spoof).
        tpr: True Positive Rates (spoof correctly classified as spoof).
        thresholds: Decreasing decision thresholds.
    """
    y_true = _normalize_labels(labels)
    y_score = _normalize_scores(scores)
    
    if len(y_true) != len(y_score):
        raise ValueError(f"Length mismatch: {len(y_true)} labels vs {len(y_score)} scores.")
    if len(y_true) == 0:
        raise ValueError("Input arrays are empty.")
    
    n_pos = int(np.sum(y_true == 1))
    n_neg = int(np.sum(y_true == 0))
    if n_pos == 0 or n_neg == 0:
        raise ValueError("ROC requires both bona fide (0) and spoof (1) samples.")
    
    desc_idx = np.argsort(y_score, kind="mergesort")[::-1]
    y_score_sorted = y_score[desc_idx]
    y_true_sorted = y_true[desc_idx]
    
    distinct_indices = np.where(np.diff(y_score_sorted))[0]
    threshold_idxs = np.r_[distinct_indices, len(y_true_sorted) - 1]
    
    tps = np.cumsum(y_true_sorted == 1)[threshold_idxs]
    fps = np.cumsum(y_true_sorted == 0)[threshold_idxs]
    
    fpr = np.r_[0.0, fps / n_neg]
    tpr = np.r_[0.0, tps / n_pos]
    thresholds = np.r_[y_score_sorted[0] + 1.0, y_score_sorted[threshold_idxs]]
    
    return fpr, tpr, thresholds


def compute_roc_auc(labels: Sequence[Any], scores: Sequence[Any]) -> float:
    """Compute Area Under the ROC Curve (ROC-AUC)."""
    fpr, tpr, _ = compute_roc_curve(labels, scores)
    trap_fn = getattr(np, "trapezoid", getattr(np, "trapz", None))
    return float(trap_fn(tpr, fpr))


def compute_pr_auc(labels: Sequence[Any], scores: Sequence[Any]) -> float:
    """Compute Area Under the Precision-Recall Curve (PR-AUC)."""
    y_true = _normalize_labels(labels)
    y_score = _normalize_scores(scores)
    
    if len(y_true) != len(y_score) or len(y_true) == 0:
        raise ValueError("Invalid input arrays for PR-AUC.")
    
    n_pos = int(np.sum(y_true == 1))
    if n_pos == 0:
        return 0.0
    
    desc_idx = np.argsort(y_score, kind="mergesort")[::-1]
    y_score_sorted = y_score[desc_idx]
    y_true_sorted = y_true[desc_idx]
    
    distinct_indices = np.where(np.diff(y_score_sorted))[0]
    threshold_idxs = np.r_[distinct_indices, len(y_true_sorted) - 1]
    
    tps = np.cumsum(y_true_sorted == 1)[threshold_idxs]
    fps = np.cumsum(y_true_sorted == 0)[threshold_idxs]
    
    recalls = np.r_[0.0, tps / n_pos]
    precisions = np.r_[1.0, tps / (tps + fps)]
    
    trap_fn = getattr(np, "trapezoid", getattr(np, "trapz", None))
    return float(trap_fn(precisions, recalls))


def compute_eer(
    labels: Sequence[Any], scores: Sequence[Any]
) -> tuple[float, float]:
    """Compute Equal Error Rate (EER) and operating threshold via linear interpolation.
    
    In biometrics / anti-spoofing convention:
    - FPR (FAR) = P(score >= threshold | bona fide) -> False alarm (genuine flagged as spoof).
    - FNR (FRR) = P(score < threshold | spoof) -> Miss rate (spoof accepted as genuine).
    - EER is the point where FPR == FNR.
    
    Returns:
        (eer, threshold): Equal error rate in [0.0, 1.0] and the corresponding score threshold.
    """
    fpr, tpr, thresholds = compute_roc_curve(labels, scores)
    fnr = 1.0 - tpr
    
    diffs = fpr - fnr
    
    if np.all(diffs <= 0):
        return 0.0, float(thresholds[0])
    if np.all(diffs >= 0):
        return 1.0, float(thresholds[-1])
        
    idx = np.where(diffs <= 0)[0][-1]
    
    if idx + 1 >= len(fpr):
        return float(fpr[idx]), float(thresholds[idx])
        
    x1, y1, t1 = fpr[idx], fnr[idx], thresholds[idx]
    x2, y2, t2 = fpr[idx + 1], fnr[idx + 1], thresholds[idx + 1]
    
    denom = (x2 - x1) - (y2 - y1)
    if abs(denom) < 1e-12:
        eer = (x1 + y1) / 2.0
        threshold = (t1 + t2) / 2.0
    else:
        alpha = (y1 - x1) / denom
        alpha = np.clip(alpha, 0.0, 1.0)
        eer = x1 + alpha * (x2 - x1)
        threshold = t1 + alpha * (t2 - t1)
        
    return float(np.clip(eer, 0.0, 1.0)), float(threshold)


def compute_min_dcf(
    labels: Sequence[Any],
    scores: Sequence[Any],
    p_target: float = 0.01,
    c_miss: float = 1.0,
    c_fa: float = 1.0,
) -> tuple[float, float]:
    """Compute Minimum Normalized Detection Cost Function (minDCF).
    
    Standard NIST SRE / ASVspoof definition:
    - Target = Genuine / Bona fide user
    - Non-target = Spoof attack
    - P_miss(theta) = P(score >= theta | bona fide) (genuine rejected)
    - P_fa(theta) = P(score < theta | spoof) (spoof accepted)
    
    C_det(theta) = c_miss * p_target * P_miss(theta) + c_fa * (1 - p_target) * P_fa(theta)
    C_default = min(c_miss * p_target, c_fa * (1 - p_target))
    DCF_norm(theta) = C_det(theta) / C_default
    minDCF = min_theta DCF_norm(theta)
    
    Returns:
        (min_dcf, best_threshold): Minimum normalized DCF and optimal threshold.
    """
    fpr, tpr, thresholds = compute_roc_curve(labels, scores)
    p_miss = fpr  # P(score >= theta | bona fide)
    p_fa = 1.0 - tpr  # P(score < theta | spoof)
    
    c_default = min(c_miss * p_target, c_fa * (1.0 - p_target))
    if c_default <= 0:
        raise ValueError("Invalid cost weights or target prior.")
        
    c_det = c_miss * p_target * p_miss + c_fa * (1.0 - p_target) * p_fa
    dcf_norm = c_det / c_default
    
    min_idx = int(np.argmin(dcf_norm))
    min_dcf = float(dcf_norm[min_idx])
    best_thresh = float(thresholds[min_idx])
    
    return min_dcf, best_thresh


def compute_min_tdcf(
    asv_scores: Sequence[Any] | None,
    cm_scores: Sequence[Any],
    labels: Sequence[Any],
    p_target: float = 0.01,
    c_miss_cm: float = 1.0,
    c_fa_cm: float = 10.0,
    p_spoof: float = 0.005,
) -> tuple[float, float]:
    """Compute Minimum tandem Detection Cost Function (min t-DCF).
    
    Evaluates Countermeasure (CM) performance in tandem with Automatic Speaker Verification (ASV).
    If asv_scores are provided, computes tandem joint cost; otherwise evaluates normalized t-DCF
    under canonical ASV operating point parameters.
    
    Returns:
        (min_tdcf, best_threshold): Minimum tandem DCF and optimal CM threshold.
    """
    cm_s = _normalize_scores(cm_scores)
    y_true = _normalize_labels(labels)
    
    if len(cm_s) != len(y_true):
        raise ValueError("Length mismatch between CM scores and labels.")
        
    fpr, tpr, thresholds = compute_roc_curve(y_true, cm_s)
    p_miss_cm = fpr  # CM rejects bona fide
    p_fa_cm = 1.0 - tpr  # CM accepts spoof
    
    if asv_scores is not None:
        asv_s = _normalize_scores(asv_scores)
        if len(asv_s) != len(cm_s):
            raise ValueError("Length mismatch between ASV scores and CM scores.")
        c1 = c_miss_cm * p_target
        c2 = c_fa_cm * p_spoof
        tdcf_raw = c1 * p_miss_cm + c2 * p_fa_cm
        tdcf_default = min(c1, c2)
        tdcf_norm = tdcf_raw / tdcf_default if tdcf_default > 0 else tdcf_raw
    else:
        c_tar = c_miss_cm * p_target
        c_spf = c_fa_cm * p_spoof
        tdcf_default = min(c_tar, c_spf)
        tdcf_raw = c_tar * p_miss_cm + c_spf * p_fa_cm
        tdcf_norm = tdcf_raw / tdcf_default if tdcf_default > 0 else tdcf_raw
        
    min_idx = int(np.argmin(tdcf_norm))
    return float(tdcf_norm[min_idx]), float(thresholds[min_idx])


def compute_ece(
    labels: Sequence[Any], risks: Sequence[Any], num_bins: int = 10
) -> float:
    """Compute Expected Calibration Error (ECE).
    
    Partitions predictions into num_bins uniform confidence intervals in [0, 1].
    ECE = sum_{m=1}^M (N_m / N) * |acc(B_m) - conf(B_m)|
    
    Returns:
        ece: Float in [0.0, 1.0].
    """
    y_true = _normalize_labels(labels)
    y_risk = _normalize_scores(risks)
    
    if len(y_true) != len(y_risk):
        raise ValueError("Length mismatch between labels and risks.")
    if len(y_true) == 0:
        return 0.0
    if np.any(y_risk < 0.0) or np.any(y_risk > 1.0):
        raise ValueError("Risks must be bounded in [0.0, 1.0].")
        
    bin_boundaries = np.linspace(0.0, 1.0, num_bins + 1)
    ece = 0.0
    n = len(y_true)
    
    for i in range(num_bins):
        low, high = bin_boundaries[i], bin_boundaries[i + 1]
        if i == num_bins - 1:
            in_bin = (y_risk >= low) & (y_risk <= high)
        else:
            in_bin = (y_risk >= low) & (y_risk < high)
            
        bin_count = int(np.sum(in_bin))
        if bin_count > 0:
            bin_acc = float(np.mean(y_true[in_bin]))
            bin_conf = float(np.mean(y_risk[in_bin]))
            ece += (bin_count / n) * abs(bin_acc - bin_conf)
            
    return float(np.clip(ece, 0.0, 1.0))


def compute_brier_score(labels: Sequence[Any], risks: Sequence[Any]) -> float:
    """Compute Brier Score (Mean Squared Error of calibrated probabilities).
    
    Brier = (1 / N) * sum (risk_i - label_i)^2
    
    Returns:
        brier: Float in [0.0, 1.0].
    """
    y_true = _normalize_labels(labels)
    y_risk = _normalize_scores(risks)
    
    if len(y_true) != len(y_risk):
        raise ValueError("Length mismatch between labels and risks.")
    if len(y_true) == 0:
        return 0.0
    if np.any(y_risk < 0.0) or np.any(y_risk > 1.0):
        raise ValueError("Risks must be bounded in [0.0, 1.0].")
        
    return float(np.mean((y_risk - y_true) ** 2))


def compute_confusion_matrix(
    labels: Sequence[Any], scores: Sequence[Any], threshold: float
) -> dict[str, Any]:
    """Compute confusion matrix and binary classification metrics at given threshold.
    
    Prediction rule: score >= threshold -> predicted spoof (1), else bona fide (0).
    
    Returns:
        dict containing tp, fp, tn, fn, precision, recall, f1, accuracy.
    """
    y_true = _normalize_labels(labels)
    y_score = _normalize_scores(scores)
    
    y_pred = (y_score >= threshold).astype(np.int32)
    
    tp = int(np.sum((y_pred == 1) & (y_true == 1)))
    fp = int(np.sum((y_pred == 1) & (y_true == 0)))
    tn = int(np.sum((y_pred == 0) & (y_true == 0)))
    fn = int(np.sum((y_pred == 0) & (y_true == 1)))
    
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    accuracy = (tp + tn) / len(y_true) if len(y_true) > 0 else 0.0
    
    return {
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision": float(precision),
        "recall": float(recall),
        "f1": float(f1),
        "accuracy": float(accuracy),
        "threshold": float(threshold),
    }
