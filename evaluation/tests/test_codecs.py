"""Unit tests for Codec Transformation & DSP Degradation Pipeline (evaluation/codecs.py)."""

import numpy as np
import pytest

from evaluation.codecs import (
    apply_codec,
    apply_codec_chain,
    g711_alaw,
    get_available_codecs,
    gsm_8k_telephony,
    opus_compression,
)


@pytest.fixture
def speech_like_signal():
    """Generate 2.56s (40,960 samples @ 16kHz) speech-like composite harmonic signal."""
    sr = 16000
    duration = 2.56
    t = np.linspace(0, duration, int(sr * duration), endpoint=False, dtype=np.float32)
    # Fundamental F0 + harmonics with spectral tilt
    f0 = 150.0
    signal = np.zeros_like(t)
    for harmonic in range(1, 20):
        freq = f0 * harmonic
        if freq < 7500:
            amp = 1.0 / (harmonic ** 1.2)
            signal += amp * np.sin(2 * np.pi * freq * t + 0.1 * harmonic)
    # Modulate envelope
    envelope = 0.5 * (1.0 + np.sin(2 * np.pi * 3.0 * t))
    signal = signal * envelope
    # Normalize to [-0.9, 0.9]
    signal = 0.9 * signal / (np.max(np.abs(signal)) + 1e-12)
    return signal


class TestGSMCodec:
    def test_gsm_shape_and_range(self, speech_like_signal):
        out = gsm_8k_telephony(speech_like_signal)
        assert len(out) == len(speech_like_signal)
        assert out.dtype == np.float32
        assert np.all(np.isfinite(out))
        assert np.max(np.abs(out)) <= 1.0

    def test_gsm_frequency_filtering(self):
        sr = 16000
        t = np.linspace(0, 1.0, sr, endpoint=False, dtype=np.float32)
        # 100 Hz (below 300Hz passband), 1000 Hz (inside passband), 5000 Hz (above 3400Hz passband)
        sig_low = np.sin(2 * np.pi * 100 * t)
        sig_mid = np.sin(2 * np.pi * 1000 * t)
        sig_high = np.sin(2 * np.pi * 5000 * t)

        out_low = gsm_8k_telephony(sig_low)
        out_mid = gsm_8k_telephony(sig_mid)
        out_high = gsm_8k_telephony(sig_high)

        # Midband should retain much more energy than out-of-band signals
        rms_low = np.sqrt(np.mean(out_low ** 2))
        rms_mid = np.sqrt(np.mean(out_mid ** 2))
        rms_high = np.sqrt(np.mean(out_high ** 2))

        # Check attenuation
        assert rms_mid > 3.0 * rms_low or np.all(np.abs(out_low) < np.abs(out_mid))
        assert rms_mid > 3.0 * rms_high or np.all(np.abs(out_high) < np.abs(out_mid))


class TestG711ALawCodec:
    def test_g711_shape_and_range(self, speech_like_signal):
        out = g711_alaw(speech_like_signal)
        assert len(out) == len(speech_like_signal)
        assert out.dtype == np.float32
        assert np.all(np.isfinite(out))
        assert np.max(np.abs(out)) <= 1.0

    def test_g711_companding_quantization(self):
        # A ramp from 0 to 1 should show 8-bit discrete quantization steps
        ramp = np.linspace(-1.0, 1.0, 8000, dtype=np.float32)
        out = g711_alaw(ramp)
        assert len(out) == len(ramp)
        # Reconstructed signal should correlate highly with input
        corr = np.corrcoef(ramp, out)[0, 1]
        assert corr > 0.95


class TestOpusCodec:
    def test_opus_shape_and_range(self, speech_like_signal):
        out = opus_compression(speech_like_signal, bitrate_kbps=24)
        assert len(out) == len(speech_like_signal)
        assert out.dtype == np.float32
        assert np.all(np.isfinite(out))
        assert np.max(np.abs(out)) <= 1.0

    def test_opus_bitrate_adaptation(self):
        sr = 16000
        t = np.linspace(0, 1.0, sr, endpoint=False, dtype=np.float32)
        sig_6k = np.sin(2 * np.pi * 6000 * t)

        out_24k = opus_compression(sig_6k, bitrate_kbps=24)
        out_8k = opus_compression(sig_6k, bitrate_kbps=8)

        rms_24k = np.sqrt(np.mean(out_24k ** 2))
        rms_8k = np.sqrt(np.mean(out_8k ** 2))

        # 6kHz is passed in wideband (24k) but cut off in narrowband (8k)
        assert rms_24k > rms_8k


class TestCodecChainingAndDispatch:
    def test_apply_codec_clean(self, speech_like_signal):
        out = apply_codec(speech_like_signal, "clean_16k")
        assert np.allclose(out, speech_like_signal, atol=1e-6)

    def test_apply_codec_chain(self, speech_like_signal):
        out = apply_codec_chain(speech_like_signal, ["opus_24k", "g711_alaw"])
        assert len(out) == len(speech_like_signal)
        assert np.all(np.isfinite(out))

    def test_apply_codec_string_chain(self, speech_like_signal):
        out = apply_codec(speech_like_signal, "opus_24k_then_g711")
        assert len(out) == len(speech_like_signal)

    def test_available_codecs(self):
        codecs = get_available_codecs()
        assert "clean_16k" in codecs
        assert "gsm_8k" in codecs
        assert "g711_alaw" in codecs
        assert "opus_24k" in codecs

    def test_invalid_codec_raises(self, speech_like_signal):
        with pytest.raises(ValueError, match="Unknown codec transformation"):
            apply_codec(speech_like_signal, "non_existent_codec")
