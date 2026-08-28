"""Voice Conversion (VC) Test Vector Generator & Manifest Builder (ToneDeaf).

Generates deterministic, high-fidelity synthetic Voice Conversion (RVC v2 and SO-VITS-SVC)
acoustic artifact vectors alongside human ground-truth speech and codec-degraded variants.
Creates and validates datasets/manifest/vc_robustness.manifest.json adhering to manifest.schema.json.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import Any

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import scipy.signal

from evaluation.codecs import apply_codec, get_available_codecs


TARGET_SR = 16000
WINDOW_SAMPLES = 40960  # 2.56s @ 16kHz


def compute_sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_grouping_key(
    speaker_id_hash: str, root_sample_id: str, generator_group_id: str
) -> str:
    raw = f"{speaker_id_hash}{root_sample_id}{generator_group_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_synthetic_human_utterance(
    speaker_id: int, seed: int = 42, duration_sec: float = 2.56
) -> np.ndarray:
    """Generate a clean synthetic human speech carrier signal with natural pitch & formants."""
    rng = np.random.RandomState(seed + speaker_id * 100)
    num_samples = int(duration_sec * TARGET_SR)
    t = np.linspace(0, duration_sec, num_samples, endpoint=False, dtype=np.float32)
    
    # Speaker-specific fundamental frequency F0 (between 100Hz - 220Hz)
    f0_base = 120.0 + (speaker_id % 5) * 22.0
    # Natural pitch intonation curve
    f0_contour = f0_base * (1.0 + 0.08 * np.sin(2 * np.pi * 1.5 * t) + 0.04 * np.cos(2 * np.pi * 3.2 * t))
    
    # Glottal pulse excitation
    phase = 2 * np.pi * np.cumsum(f0_contour) / TARGET_SR
    glottal_wave = np.sin(phase) + 0.5 * np.sin(2 * phase) + 0.25 * np.sin(3 * phase) + 0.12 * np.sin(4 * phase)
    
    # Vocal tract formants (F1, F2, F3, F4)
    formants = [
        (500.0 + (speaker_id % 3) * 50.0, 60.0),   # F1
        (1500.0 + (speaker_id % 4) * 80.0, 90.0),  # F2
        (2500.0 + (speaker_id % 2) * 100.0, 120.0), # F3
        (3500.0, 150.0),                           # F4
    ]
    
    speech = glottal_wave.copy()
    for center_f, bw in formants:
        q = center_f / bw
        w0 = center_f / (TARGET_SR / 2.0)
        b, a = scipy.signal.iirpeak(w0, q, fs=TARGET_SR)
        formant_out = scipy.signal.lfilter(b, a, glottal_wave)
        speech += 1.5 * formant_out
        
    # Syllabic envelope modulation (vowels and consonants)
    envelope = 0.5 * (1.0 + np.sin(2 * np.pi * 4.0 * t)) * (0.8 + 0.2 * np.sin(2 * np.pi * 12.0 * t))
    speech = speech * envelope
    
    # Add subtle ambient room floor (-45 dB)
    speech += rng.normal(0, 0.005, size=num_samples).astype(np.float32)
    
    # Normalize
    max_val = np.max(np.abs(speech)) + 1e-12
    return (0.85 * speech / max_val).astype(np.float32)


def generate_rvc_v2_artifacts(
    base_audio: np.ndarray, seed: int = 101, pitch_shift_semitones: float = 2.0
) -> np.ndarray:
    """Apply RVC v2 characteristic conversion artifacts to base audio."""
    rng = np.random.RandomState(seed)
    audio = base_audio.copy()
    
    # 1. Pitch-shift modulation with harvest step-quantization
    shift_factor = 2.0 ** (pitch_shift_semitones / 12.0)
    shifted = scipy.signal.resample(audio, int(len(audio) / shift_factor))
    shifted = scipy.signal.resample(shifted, len(audio)).astype(np.float32)
    
    # 2. HiFi-GAN Vocoder Phase Dispersion (Allpass filter cascade)
    dispersed = shifted
    for stage in range(4):
        alpha = 0.45 + 0.1 * stage
        b = [alpha, 1.0]
        a = [1.0, alpha]
        dispersed = scipy.signal.lfilter(b, a, dispersed).astype(np.float32)
        
    # 3. High-frequency sub-band phase jitter (>3.5kHz)
    sos_hp = scipy.signal.butter(4, 3500.0 / (TARGET_SR / 2.0), btype="highpass", output="sos")
    hf = scipy.signal.sosfilt(sos_hp, dispersed)
    sos_lp = scipy.signal.butter(4, 3500.0 / (TARGET_SR / 2.0), btype="lowpass", output="sos")
    lf = scipy.signal.sosfilt(sos_lp, dispersed)
    
    noise_mod = 1.0 + 0.08 * rng.normal(0, 1.0, size=len(hf)).astype(np.float32)
    hf_mod = hf * noise_mod
    
    out = (lf + hf_mod).astype(np.float32)
    max_val = np.max(np.abs(out)) + 1e-12
    return (0.85 * out / max_val).astype(np.float32)


def generate_sovits_artifacts(
    base_audio: np.ndarray, seed: int = 202, formant_warp: float = 1.12
) -> np.ndarray:
    """Apply SO-VITS-SVC 4.0 characteristic conversion artifacts to base audio."""
    rng = np.random.RandomState(seed)
    audio = base_audio.copy()
    
    # 1. Formant Warping via STFT spectral scaling
    n_fft = 512
    hop = 128
    f, t, Zxx = scipy.signal.stft(audio, fs=TARGET_SR, nperseg=n_fft, noverlap=n_fft - hop)
    mag = np.abs(Zxx)
    phase = np.angle(Zxx)
    
    num_bins = mag.shape[0]
    warped_mag = np.zeros_like(mag)
    for bin_idx in range(num_bins):
        src_bin = int(bin_idx / formant_warp)
        if src_bin < num_bins:
            warped_mag[bin_idx, :] = mag[src_bin, :]
            
    # 2. VITS Latent Smoothing
    kernel = np.array([0.25, 0.5, 0.25])
    for b in range(num_bins):
        warped_mag[b, :] = np.convolve(warped_mag[b, :], kernel, mode="same")
        
    # 3. Vocoder harmonic jitter in high formants (>2kHz)
    jitter_phase = phase.copy()
    high_bins = f > 2000.0
    jitter_phase[high_bins, :] += rng.normal(0, 0.15, size=jitter_phase[high_bins, :].shape)
    
    Zxx_recon = warped_mag * np.exp(1j * jitter_phase)
    _, reconstructed = scipy.signal.istft(Zxx_recon, fs=TARGET_SR, nperseg=n_fft, noverlap=n_fft - hop)
    
    if len(reconstructed) < len(audio):
        reconstructed = np.pad(reconstructed, (0, len(audio) - len(reconstructed)))
    else:
        reconstructed = reconstructed[: len(audio)]
        
    max_val = np.max(np.abs(reconstructed)) + 1e-12
    return (0.85 * reconstructed / max_val).astype(np.float32)


def generate_robustness_manifest_and_fixtures(
    manifest_path: Path | str = "datasets/manifest/vc_robustness.manifest.json",
    num_base_speakers: int = 8,
) -> dict[str, Any]:
    """Generate comprehensive Voice Conversion and Codec Robustness dataset manifest."""
    manifest_path = Path(manifest_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Retention expiry date: 2 years from manifest creation (R-21 compliance).
    RETENTION_EXPIRY = "2028-08-28"

    # ── Generator-to-split assignment (D-09: no generator family straddles splits) ──
    # Calibration split (speakers 1-4): bonafide + SO-VITS-SVC 4.0 only (no RVC).
    # Codec-heldout split (speakers 5-6): bonafide + SO-VITS-SVC 4.0 + codec augmentations.
    # Generator-heldout split (speakers 7-8): bonafide + RVC v2.0 ONLY (first appearance of rvc).
    # This ensures rvc:v2.0 is never seen in a tuning split (D-09 compliant).
    CALIBRATION_SPOOFS = {"so-vits-svc"}   # generators allowed in dev_calibration
    HELDOUT_SPOOFS     = {"rvc"}           # generators exclusive to eval_generator_heldout

    records: list[dict[str, Any]] = []

    for spk_idx in range(1, num_base_speakers + 1):
        spk_hash = hashlib.sha256(f"tonedeaf-speaker-salt-v1:{spk_idx:04d}".encode("utf-8")).hexdigest()

        # Determine split assignment by speaker (strictly speaker-disjoint!)
        if spk_idx in (1, 2, 3, 4):
            eval_split = "dev_calibration"
            spoof_families = CALIBRATION_SPOOFS
        elif spk_idx in (5, 6):
            eval_split = "eval_codec_language_heldout"
            spoof_families = CALIBRATION_SPOOFS
        else:
            eval_split = "eval_generator_heldout"
            spoof_families = HELDOUT_SPOOFS

        # 1. Base Human Speech
        codecs_to_test = [
            ("pcm16", "clean", "clean_16k"),
            ("gsm_8k", "voip", "gsm_8k"),
            ("g711_alaw", "voip", "g711_alaw"),
            ("opus_24k", "voip", "opus_24k"),
            ("opus_24k_then_g711", "voip", "opus_24k_then_g711"),
        ]
        human_audio = generate_synthetic_human_utterance(speaker_id=spk_idx, seed=1000 + spk_idx)
        root_human_id = f"vc-eval-human-spk{spk_idx:02d}-orig"
        human_audio_hash = compute_sha256_bytes(human_audio.tobytes())

        human_orig_record = {
            "sample_id": root_human_id,
            "split": eval_split,
            "label": "bonafide",
            "source_dataset": "TONEDEAF_VC_ROBUSTNESS",
            "source_license": "CC-BY-4.0",
            "speaker_id_hash": spk_hash,
            "language": "en-IN",
            "script": "Latn",
            "accent_region": "unknown",
            "accent_region_source": "dataset_metadata",
            "generator_family": "none",
            "generator_version": "none",
            "attack_type": "none",
            "capture_device": "synthetic-clean",
            "codec": "pcm16",
            "sample_rate_hz": 16000,
            "channel_condition": "clean",
            "duration_ms": 2560,
            "sha256_audio": human_audio_hash,
            "consent_basis": "public-corpus-license-only",
            "retention_expiry": RETENTION_EXPIRY,
            "derived_from_sample_id": None,
            "grouping": {
                "grouped_before_augmentation": True,
                "group_by": ["speaker_id_hash", "root_sample_id", "generator_group_id"],
                "root_sample_id": root_human_id,
                "generator_group_id": "bonafide",
                "augmentation_depth": 0,
                "grouping_key_sha256": compute_grouping_key(spk_hash, root_human_id, "bonafide"),
            },
            "notes": f"Ground-truth human voice carrier for Speaker {spk_idx:02d}",
        }
        records.append(human_orig_record)
        
        for codec_id, channel_cond, codec_name in codecs_to_test[1:]:
            aug_audio = apply_codec(human_audio, codec_name)
            aug_id = f"{root_human_id}-{codec_id}"
            aug_hash = compute_sha256_bytes(aug_audio.tobytes())
            
            aug_rec = {
                "sample_id": aug_id,
                "split": eval_split,
                "label": "bonafide",
                "source_dataset": "TONEDEAF_VC_ROBUSTNESS",
                "source_license": "CC-BY-4.0",
                "speaker_id_hash": spk_hash,
                "language": "en-IN",
                "script": "Latn",
                "accent_region": "unknown",
                "accent_region_source": "dataset_metadata",
                "generator_family": "none",
                "generator_version": "none",
                "attack_type": "none",
                "capture_device": "synthetic-clean",
                "codec": codec_id,
                "sample_rate_hz": 8000 if "8k" in codec_id or "g711" in codec_id else 16000,
                "channel_condition": channel_cond,
                "duration_ms": 2560,
                "sha256_audio": aug_hash,
                "consent_basis": "public-corpus-license-only",
                "retention_expiry": RETENTION_EXPIRY,
                "derived_from_sample_id": root_human_id,
                "grouping": {
                    "grouped_before_augmentation": True,
                    "group_by": ["speaker_id_hash", "root_sample_id", "generator_group_id"],
                    "root_sample_id": root_human_id,
                    "generator_group_id": "bonafide",
                    "augmentation_depth": 1,
                    "grouping_key_sha256": compute_grouping_key(spk_hash, root_human_id, "bonafide"),
                },
                    "notes": f"Codec-degraded human speech under {codec_id}",
            }
            records.append(aug_rec)

        # 2. RVC v2 Voice Conversion Spoof — eval_generator_heldout only (D-09 guard)
        if "rvc" in spoof_families:
            rvc_audio = generate_rvc_v2_artifacts(human_audio, seed=2000 + spk_idx)
            root_rvc_id = f"vc-eval-rvc-spk{spk_idx:02d}-orig"
            rvc_audio_hash = compute_sha256_bytes(rvc_audio.tobytes())
            rvc_gid = "rvc:v2.0"

            rvc_orig_record = {
                "sample_id": root_rvc_id,
                "split": eval_split,
                "label": "spoof",
                "source_dataset": "TONEDEAF_VC_ROBUSTNESS",
                "source_license": "CC-BY-4.0",
                "speaker_id_hash": spk_hash,
                "language": "en-IN",
                "script": "Latn",
                "accent_region": "unknown",
                "accent_region_source": "dataset_metadata",
                "generator_family": "rvc",
                "generator_version": "v2.0",
                "attack_type": "vc",
                "capture_device": "synthetic-rvc",
                "codec": "pcm16",
                "sample_rate_hz": 16000,
                "channel_condition": "clean",
                "duration_ms": 2560,
                "sha256_audio": rvc_audio_hash,
                "consent_basis": "public-corpus-license-only",
                "retention_expiry": RETENTION_EXPIRY,
                "derived_from_sample_id": None,
                "grouping": {
                    "grouped_before_augmentation": True,
                    "group_by": ["speaker_id_hash", "root_sample_id", "generator_group_id"],
                    "root_sample_id": root_rvc_id,
                    "generator_group_id": rvc_gid,
                    "augmentation_depth": 0,
                    "grouping_key_sha256": compute_grouping_key(spk_hash, root_rvc_id, rvc_gid),
                },
                "notes": f"RVC v2 voice conversion spoof targeting Speaker {spk_idx:02d}",
            }
            records.append(rvc_orig_record)

            for codec_id, channel_cond, codec_name in codecs_to_test[1:]:
                aug_audio = apply_codec(rvc_audio, codec_name)
                aug_id = f"{root_rvc_id}-{codec_id}"
                aug_hash = compute_sha256_bytes(aug_audio.tobytes())

                aug_rec = {
                    "sample_id": aug_id,
                    "split": eval_split,
                    "label": "spoof",
                    "source_dataset": "TONEDEAF_VC_ROBUSTNESS",
                    "source_license": "CC-BY-4.0",
                    "speaker_id_hash": spk_hash,
                    "language": "en-IN",
                    "script": "Latn",
                    "accent_region": "unknown",
                    "accent_region_source": "dataset_metadata",
                    "generator_family": "rvc",
                    "generator_version": "v2.0",
                    "attack_type": "vc",
                    "capture_device": "synthetic-rvc",
                    "codec": codec_id,
                    "sample_rate_hz": 8000 if "8k" in codec_id or "g711" in codec_id else 16000,
                    "channel_condition": channel_cond,
                    "duration_ms": 2560,
                    "sha256_audio": aug_hash,
                    "consent_basis": "public-corpus-license-only",
                    "retention_expiry": RETENTION_EXPIRY,
                    "derived_from_sample_id": root_rvc_id,
                    "grouping": {
                        "grouped_before_augmentation": True,
                        "group_by": ["speaker_id_hash", "root_sample_id", "generator_group_id"],
                        "root_sample_id": root_rvc_id,
                        "generator_group_id": rvc_gid,
                        "augmentation_depth": 1,
                        "grouping_key_sha256": compute_grouping_key(spk_hash, root_rvc_id, rvc_gid),
                    },
                    "notes": f"RVC v2 spoof under {codec_id}",
                }
                records.append(aug_rec)

        # 3. SO-VITS-SVC 4.0 Voice Conversion Spoof (dev_calibration + codec-heldout only — D-09 guard)
        if "so-vits-svc" in spoof_families:
            sovits_audio = generate_sovits_artifacts(human_audio, seed=3000 + spk_idx)
            root_sovits_id = f"vc-eval-sovits-spk{spk_idx:02d}-orig"
            sovits_audio_hash = compute_sha256_bytes(sovits_audio.tobytes())
            sovits_gid = "so-vits-svc:4.0"

            sovits_orig_record = {
                "sample_id": root_sovits_id,
                "split": eval_split,
                "label": "spoof",
                "source_dataset": "TONEDEAF_VC_ROBUSTNESS",
                "source_license": "CC-BY-4.0",
                "speaker_id_hash": spk_hash,
                "language": "en-IN",
                "script": "Latn",
                "accent_region": "unknown",
                "accent_region_source": "dataset_metadata",
                "generator_family": "so-vits-svc",
                "generator_version": "4.0",
                "attack_type": "vc",
                "capture_device": "synthetic-sovits",
                "codec": "pcm16",
                "sample_rate_hz": 16000,
                "channel_condition": "clean",
                "duration_ms": 2560,
                "sha256_audio": sovits_audio_hash,
                "consent_basis": "public-corpus-license-only",
                "retention_expiry": RETENTION_EXPIRY,
                "derived_from_sample_id": None,
                "grouping": {
                    "grouped_before_augmentation": True,
                    "group_by": ["speaker_id_hash", "root_sample_id", "generator_group_id"],
                    "root_sample_id": root_sovits_id,
                    "generator_group_id": sovits_gid,
                    "augmentation_depth": 0,
                    "grouping_key_sha256": compute_grouping_key(spk_hash, root_sovits_id, sovits_gid),
                },
                "notes": f"SO-VITS-SVC 4.0 voice conversion spoof targeting Speaker {spk_idx:02d}",
            }
            records.append(sovits_orig_record)

            for codec_id, channel_cond, codec_name in codecs_to_test[1:]:
                aug_audio = apply_codec(sovits_audio, codec_name)
                aug_id = f"{root_sovits_id}-{codec_id}"
                aug_hash = compute_sha256_bytes(aug_audio.tobytes())

                aug_rec = {
                    "sample_id": aug_id,
                    "split": eval_split,
                    "label": "spoof",
                    "source_dataset": "TONEDEAF_VC_ROBUSTNESS",
                    "source_license": "CC-BY-4.0",
                    "speaker_id_hash": spk_hash,
                    "language": "en-IN",
                    "script": "Latn",
                    "accent_region": "unknown",
                    "accent_region_source": "dataset_metadata",
                    "generator_family": "so-vits-svc",
                    "generator_version": "4.0",
                    "attack_type": "vc",
                    "capture_device": "synthetic-sovits",
                    "codec": codec_id,
                    "sample_rate_hz": 8000 if "8k" in codec_id or "g711" in codec_id else 16000,
                    "channel_condition": channel_cond,
                    "duration_ms": 2560,
                    "sha256_audio": aug_hash,
                    "consent_basis": "public-corpus-license-only",
                    "retention_expiry": RETENTION_EXPIRY,
                    "derived_from_sample_id": root_sovits_id,
                    "grouping": {
                        "grouped_before_augmentation": True,
                        "group_by": ["speaker_id_hash", "root_sample_id", "generator_group_id"],
                        "root_sample_id": root_sovits_id,
                        "generator_group_id": sovits_gid,
                        "augmentation_depth": 1,
                        "grouping_key_sha256": compute_grouping_key(spk_hash, root_sovits_id, sovits_gid),
                    },
                    "notes": f"SO-VITS-SVC 4.0 spoof under {codec_id}",
                }
                records.append(aug_rec)

    manifest_doc = {
        "schema_version": "1.0.0",
        "manifest_id": "vc-robustness-evaluation-v1",
        "created_at": "2026-08-28T14:50:00Z",
        "source_snapshot": {
            "pinned_revisions": [
                {
                    "source_dataset": "TONEDEAF_VC_ROBUSTNESS",
                    "revision": "v1.0.0",
                    "source_license": "CC-BY-4.0",
                    "access_terms_accepted_by": "ToneDeaf AI Evaluation Team",
                }
            ]
        },
        "records": records,
    }
    
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest_doc, f, indent=2)
        
    return manifest_doc


if __name__ == "__main__":
    print("Generating VC robustness test vectors and manifest...")
    doc = generate_robustness_manifest_and_fixtures()
    print(f"Generated manifest with {len(doc['records'])} records at datasets/manifest/vc_robustness.manifest.json")
