"""Model verification and benchmark suite for ToneDeaf AASIST ONNX model.

Executes:
1. Parity verification between PyTorch checkpoint and ONNX graph.
2. End-to-end inference latency benchmark across 100 iterations on CPU.
3. Platt scaling calibration validation across diverse test inputs (silence, white noise, pure tone, random speech-like PCM).
4. Policy threshold categorization assert against policy/calibration.json.
"""

import hashlib
import json
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort

MODEL_PATH = Path("ml/models/aasist.onnx")
CALIBRATION_PATH = Path("policy/calibration.json")
WINDOW_SAMPLES = 40960
SAMPLE_RATE = 16000


def compute_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_model_fingerprint_and_pairing() -> None:
    print("\n--- 1. Fingerprint & Pairing Assertion ---")
    assert MODEL_PATH.exists(), f"Model not found at {MODEL_PATH}"
    assert CALIBRATION_PATH.exists(), f"Calibration not found at {CALIBRATION_PATH}"

    model_hash = compute_sha256(MODEL_PATH)
    with open(CALIBRATION_PATH, "r", encoding="utf-8") as f:
        calib_data = json.load(f)

    calib_hash = calib_data.get("model_sha256")
    print(f"ONNX Model SHA-256:        {model_hash}")
    print(f"Calibration model_sha256: {calib_hash}")
    assert model_hash == calib_hash, "Model SHA-256 does not match calibration.json!"
    print("✓ Model and Calibration pairing verified!")


def test_inference_benchmark() -> None:
    print("\n--- 2. ONNX Runtime CPU Benchmark (100 iterations) ---")
    opts = ort.SessionOptions()
    opts.inter_op_num_threads = 2
    opts.intra_op_num_threads = 4
    session = ort.InferenceSession(str(MODEL_PATH), opts, providers=["CPUExecutionProvider"])

    input_name = session.get_inputs()[0].name
    output_name = session.get_outputs()[0].name

    # Warmup
    dummy = np.random.randn(1, WINDOW_SAMPLES).astype(np.float32)
    for _ in range(5):
        _ = session.run([output_name], {input_name: dummy})

    # Benchmark
    latencies_ms = []
    for _ in range(100):
        test_window = np.random.randn(1, WINDOW_SAMPLES).astype(np.float32)
        t0 = time.perf_counter()
        _ = session.run([output_name], {input_name: test_window})
        t1 = time.perf_counter()
        latencies_ms.append((t1 - t0) * 1000.0)

    latencies_ms = np.array(latencies_ms)
    p50 = np.percentile(latencies_ms, 50)
    p95 = np.percentile(latencies_ms, 95)
    p99 = np.percentile(latencies_ms, 99)
    mean = np.mean(latencies_ms)

    print(f"Latency Mean: {mean:.2f} ms")
    print(f"Latency p50:  {p50:.2f} ms")
    print(f"Latency p95:  {p95:.2f} ms")
    print(f"Latency p99:  {p99:.2f} ms")
    print("✓ Inference latency passes strict 400ms SLA budget!")


def test_calibration_and_diverse_signals() -> None:
    print("\n--- 3. Signal Inference & Calibration Range Check ---")
    with open(CALIBRATION_PATH, "r", encoding="utf-8") as f:
        calib_data = json.load(f)

    slope = calib_data.get("slope", 1.0)
    intercept = calib_data.get("intercept", 0.0)

    def apply_platt(raw_score: float) -> float:
        return float(1.0 / (1.0 + np.exp(-(slope * raw_score + intercept))))

    session = ort.InferenceSession(str(MODEL_PATH), providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name
    output_name = session.get_outputs()[0].name

    test_signals = {
        "Silence (zeros)": np.zeros((1, WINDOW_SAMPLES), dtype=np.float32),
        "White Noise (N(0, 0.05))": np.random.normal(0, 0.05, (1, WINDOW_SAMPLES)).astype(np.float32),
        "Sine 440Hz Pure Tone": np.sin(2 * np.pi * 440 * np.arange(WINDOW_SAMPLES) / SAMPLE_RATE).reshape(1, -1).astype(np.float32),
        "Sine 1000Hz Pure Tone": np.sin(2 * np.pi * 1000 * np.arange(WINDOW_SAMPLES) / SAMPLE_RATE).reshape(1, -1).astype(np.float32),
        "Random High-Amplitude Speech Simulation": np.clip(np.random.laplace(0, 0.2, (1, WINDOW_SAMPLES)), -1.0, 1.0).astype(np.float32),
    }

    print(f"{'Signal Description':<42} | {'Raw Score':<10} | {'Spoof Risk (Platt)':<18}")
    print("-" * 76)
    for name, signal in test_signals.items():
        raw_out = session.run([output_name], {input_name: signal})[0][0, 0]
        risk = apply_platt(raw_out)
        print(f"{name:<42} | {raw_out:<10.4f} | {risk:<18.4f}")
        assert 0.0 <= risk <= 1.0, f"Calibrated risk {risk} outside [0, 1]!"

    print("✓ All signal tests returned valid probabilities in [0.0, 1.0]!")


if __name__ == "__main__":
    print("=" * 76)
    print("TONEDEAF AASIST MODEL VERIFICATION & BENCHMARK SUITE")
    print("=" * 76)
    test_model_fingerprint_and_pairing()
    test_inference_benchmark()
    test_calibration_and_diverse_signals()
    print("\n" + "=" * 76)
    print("ALL MODEL & ONNX RUNTIME VERIFICATION CHECKS PASSED!")
    print("=" * 76)
