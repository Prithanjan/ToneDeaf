#!/usr/bin/env python3
"""ToneDeaf Live Audio Sample Scorer & Deepfake Diagnostics.

Accepts audio files (.wav, .flac, .ogg, .mp3, .npy, .raw), resamples to 16kHz mono,
slices into 2.56s (40,960 sample) windows, evaluates through AASIST ONNX detector,
applies Platt scaling, and prints real-time spoof diagnostics.

Usage:
    python ml/src/score_sample.py <path-to-audio-file-or-dir> [--hop 0.64]
"""

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
import soundfile as sf
import scipy.signal

TARGET_SR = 16000
WINDOW_SAMPLES = 40960
MODEL_PATH = Path("ml/models/aasist.onnx")
CALIBRATION_PATH = Path("policy/calibration.json")


def load_audio(path: Path) -> tuple[np.ndarray, float]:
    """Load audio file, convert to 16kHz mono float32 normalized [-1, 1]."""
    if path.suffix.lower() == ".npy":
        arr = np.load(path).astype(np.float32)
        if arr.ndim > 1:
            arr = arr.flatten()
        duration = len(arr) / TARGET_SR
        return arr, duration

    data, sr = sf.read(path, dtype="float32")
    if data.ndim > 1:
        data = np.mean(data, axis=1)  # Stereo to mono

    if sr != TARGET_SR:
        num_target_samples = int(len(data) * TARGET_SR / sr)
        data = scipy.signal.resample(data, num_target_samples).astype(np.float32)

    duration = len(data) / TARGET_SR
    return data, duration


def prepare_windows(audio: np.ndarray, hop_sec: float) -> list[tuple[float, float, np.ndarray]]:
    """Slice audio into 2.56s windows with hop_sec step."""
    hop_samples = int(hop_sec * TARGET_SR)
    total_samples = len(audio)
    windows = []

    if total_samples <= WINDOW_SAMPLES:
        # Pad or repeat if audio is shorter than 2.56s
        if total_samples < WINDOW_SAMPLES:
            repeats = int(np.ceil(WINDOW_SAMPLES / total_samples))
            padded = np.tile(audio, repeats)[:WINDOW_SAMPLES]
        else:
            padded = audio
        windows.append((0.0, total_samples / TARGET_SR, padded.reshape(1, WINDOW_SAMPLES)))
        return windows

    start = 0
    while start + WINDOW_SAMPLES <= total_samples:
        t_start = start / TARGET_SR
        t_end = (start + WINDOW_SAMPLES) / TARGET_SR
        chunk = audio[start : start + WINDOW_SAMPLES].reshape(1, WINDOW_SAMPLES)
        windows.append((t_start, t_end, chunk))
        start += hop_samples

    # If remainder at end
    if start < total_samples and total_samples - start > hop_samples // 2:
        chunk = audio[-WINDOW_SAMPLES:].reshape(1, WINDOW_SAMPLES)
        t_start = (total_samples - WINDOW_SAMPLES) / TARGET_SR
        t_end = total_samples / TARGET_SR
        windows.append((t_start, t_end, chunk))

    return windows


def classify_risk(risk: float) -> tuple[str, str]:
    if risk < 0.35:
        return "ALLOW", "🟢 GENUINE / BONAFIDE"
    elif risk < 0.65:
        return "MONITOR", "🟡 UNCERTAIN / LOW CONFIDENCE"
    elif risk < 0.78:
        return "HOLD", "🟠 SUSPICIOUS / HOLD CALL"
    elif risk < 0.90:
        return "ESCALATE", "🔴 HIGH SPOOF PROBABILITY"
    else:
        return "REJECT", "⛔ CONFIRMED DEEPFAKE SPOOF"


def score_file(path: Path, session: ort.InferenceSession, slope: float, intercept: float, hop_sec: float) -> None:
    print("\n" + "=" * 88)
    print(f"DIAGNOSTIC ANALYSIS: {path.name}")
    print("=" * 88)

    try:
        audio, duration = load_audio(path)
    except Exception as e:
        print(f"Error loading {path}: {e}")
        return

    windows = prepare_windows(audio, hop_sec)
    print(f"Audio Duration: {duration:.2f}s | Sample Rate: 16,000 Hz | Windows Extracted: {len(windows)}")
    print(f"Platt Calibration: slope={slope:.4f}, intercept={intercept:.4f}")
    print("-" * 88)
    print(f"{'Window':<8} | {'Time Range':<15} | {'Raw Score':<11} | {'Spoof Risk':<12} | {'Action':<10} | {'Verdict'}")
    print("-" * 88)

    input_name = session.get_inputs()[0].name
    output_name = session.get_outputs()[0].name

    raw_scores = []
    risks = []
    actions = []

    for idx, (t0, t1, chunk) in enumerate(windows, 1):
        t_start = time.perf_counter()
        raw = float(session.run([output_name], {input_name: chunk})[0][0, 0])
        inf_ms = (time.perf_counter() - t_start) * 1000.0

        # Platt scaling: 1 / (1 + exp(-(slope * raw + intercept)))
        risk = float(1.0 / (1.0 + np.exp(-(slope * raw + intercept))))
        action, verdict = classify_risk(risk)

        raw_scores.append(raw)
        risks.append(risk)
        actions.append(action)

        time_str = f"{t0:.2f}s - {t1:.2f}s"
        print(f"#{idx:<7} | {time_str:<15} | {raw:<11.4f} | {risk*100:>5.1f}%      | {action:<10} | {verdict} ({inf_ms:.1f}ms)")

    print("-" * 88)
    avg_risk = float(np.mean(risks))
    max_risk = float(np.max(risks))
    overall_action, overall_verdict = classify_risk(avg_risk)

    print(f"OVERALL SUMMARY:")
    print(f"  • Average Spoof Risk:  {avg_risk*100:.1f}%")
    print(f"  • Peak Window Risk:    {max_risk*100:.1f}%")
    print(f"  • Primary Action:      {overall_action}")
    print(f"  • Overall Verdict:     {overall_verdict}")
    print("=" * 88)


def main():
    parser = argparse.ArgumentParser(description="ToneDeaf Live Audio Sample Scorer")
    parser.add_argument("path", type=Path, help="Audio file or directory containing samples")
    parser.add_argument("--hop", type=float, default=0.64, help="Hop duration in seconds (default: 0.64s)")
    args = parser.parse_args()

    if not MODEL_PATH.exists():
        print(f"Error: Model not found at {MODEL_PATH}")
        sys.exit(1)

    with open(CALIBRATION_PATH, "r", encoding="utf-8") as f:
        calib = json.load(f)
    slope = calib.get("slope", 1.0)
    intercept = calib.get("intercept", 0.0)

    opts = ort.SessionOptions()
    opts.inter_op_num_threads = 2
    opts.intra_op_num_threads = 4
    session = ort.InferenceSession(str(MODEL_PATH), opts, providers=["CPUExecutionProvider"])

    target = args.path
    if target.is_dir():
        supported = [".wav", ".flac", ".ogg", ".mp3", ".npy"]
        files = [p for p in target.rglob("*") if p.suffix.lower() in supported]
        if not files:
            print(f"No audio files found in {target}")
            return
        for f in files:
            score_file(f, session, slope, intercept, args.hop)
    else:
        score_file(target, session, slope, intercept, args.hop)


if __name__ == "__main__":
    main()
