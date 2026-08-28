#!/usr/bin/env python3
"""ONNX Exporter for AASIST Voice-Clone Detector.

Exports best_mixed_finetune_256s_v2 PyTorch model to ONNX format.
Enforces the repository contracts:
  - Input: [1, 40960] float32 raw waveform (2.56s @ 16kHz)
  - Output: [1, 1] float32 scalar (negated bonafide logit: -out[:, 1], higher = spoof)
  - PyTorch vs ONNX Runtime parity assertion (< 1e-4 tolerance)
"""

import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np
import onnx
import onnxruntime as ort
import torch
import torch.nn as nn

REPO_ROOT = Path('/workspace')
sys.path.insert(0, str(REPO_ROOT / 'ml' / 'src'))
from models.AASIST import Model


class AasistSpoofDetectorWrapper(nn.Module):
    """Wrapper that wraps AASIST to output a single spoof-oriented scalar."""

    def __init__(self, model: nn.Module):
        super().__init__()
        self.model = model

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x shape: [batch, 40960]
        _, output = self.model(x)
        # output shape: [batch, 2] -> index 0 is spoof-leaning in raw loss, index 1 is bonafide-leaning.
        # Negate index 1 so that higher score strictly indicates higher spoof probability:
        # score = -output[:, 1:2] (shape: [batch, 1])
        score = -output[:, 1:2]
        return score


def main():
    print("=== Step 1: Loading PyTorch AASIST Model ===")
    config_path = REPO_ROOT / 'ml' / 'configs' / 'AASIST.conf'
    with open(config_path) as f:
        conf = json.load(f)

    base_model = Model(conf['model_config'])
    ckpt_path = REPO_ROOT / 'best_mixed_finetune_256s_v2.pth.zip'
    state_dict = torch.load(ckpt_path, map_location='cpu')
    base_model.load_state_dict(state_dict, strict=True)
    base_model.eval()

    wrapper = AasistSpoofDetectorWrapper(base_model)
    wrapper.eval()

    print("=== Step 2: Exporting to ONNX ===")
    out_dir = REPO_ROOT / 'ml' / 'models'
    out_dir.mkdir(parents=True, exist_ok=True)
    onnx_path = out_dir / 'aasist.onnx'

    dummy_input = torch.randn(1, 40960, dtype=torch.float32)

    with torch.no_grad():
        torch_out = wrapper(dummy_input).numpy()

    torch.onnx.export(
        wrapper,
        dummy_input,
        str(onnx_path),
        export_params=True,
        opset_version=17,
        do_constant_folding=True,
        input_names=['waveform'],
        output_names=['score'],
        dynamic_axes={
            'waveform': {0: 'batch_size'},
            'score': {0: 'batch_size'}
        },
        dynamo=False
    )

    print(f"✓ ONNX model exported to: {onnx_path}")

    print("=== Step 3: Validating ONNX graph ===")
    onnx_model = onnx.load(str(onnx_path))
    onnx.checker.check_model(onnx_model)
    print("✓ onnx.checker passed!")

    print("=== Step 4: Testing ONNX Runtime Parity ===")
    session = ort.InferenceSession(str(onnx_path), providers=['CPUExecutionProvider'])
    ort_inputs = {'waveform': dummy_input.numpy()}
    ort_out = session.run(None, ort_inputs)[0]

    print("PyTorch output shape:", torch_out.shape, "val:", torch_out)
    print("ONNX Runtime shape:  ", ort_out.shape, "val:", ort_out)
    max_diff = np.max(np.abs(torch_out - ort_out))
    print(f"Max absolute difference: {max_diff:.2e}")
    assert max_diff < 1e-4, f"Parity check failed! max diff = {max_diff}"
    print("✓ PyTorch vs ONNX Runtime parity verified (< 1e-4)!")

    print("=== Step 5: Scoring Contract Vector Fixture ===")
    fixture_path = REPO_ROOT / 'ml' / 'fixtures' / 'contract_vector_v1.npy'
    if fixture_path.exists():
        vec = np.load(str(fixture_path))
        if vec.ndim == 1:
            vec = vec.reshape(1, -1)
        fixture_score = session.run(None, {'waveform': vec.astype(np.float32)})[0]
        print(f"Contract vector fixture score: {fixture_score[0, 0]:.6f}")

    print("=== Step 6: Computing SHA-256 Fingerprint ===")
    h = hashlib.sha256()
    with open(onnx_path, 'rb') as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    model_sha = h.hexdigest()
    print(f"model_sha256: {model_sha}")

    # Output json summary
    summary = {
        'model_file': 'aasist.onnx',
        'model_sha256': model_sha,
        'input_shape': [1, 40960],
        'output_shape': [1, 1],
        'max_diff': float(max_diff),
    }
    with open(REPO_ROOT / 'ml' / 'export_summary.json', 'w') as f:
        json.dump(summary, f, indent=2)
    print("✓ Export summary written to ml/export_summary.json")


if __name__ == '__main__':
    main()
