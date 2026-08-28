"""DSP Codec Transformations & Audio Degradation Pipeline for Voice Integrity Control Plane (ToneDeaf).

Implements pure Python/NumPy/SciPy zero-external-binary codec simulations:
1. gsm_8k_telephony: 8kHz downsampling, 300Hz-3400Hz Butterworth bandpass filter,
   LPC (Linear Predictive Coding) 10th-order analysis-synthesis and residual quantization,
   upsampled back to 16kHz float32.
2. g711_alaw: ITU-T Recommendation G.711 A-law (A=87.6) 8-bit logarithmic companding @ 8kHz,
   reconstructed to float32 16kHz.
3. opus_compression: VoIP Opus simulation featuring bitrate-adaptive bandwidth filtering
   (Wideband / Mediumband / Narrowband), psychoacoustic critical-band spectral masking,
   and frame-based quantization noise.
4. apply_codec_chain: Multi-hop cascading codec degradation (e.g. Opus 24k -> G.711 A-law).

All functions preserve input length, float32 normalization in [-1.0, 1.0], and handle arbitrary audio durations.
"""

from __future__ import annotations

from typing import Sequence
import numpy as np
import scipy.signal


TARGET_SR = 16000


def _ensure_float32_mono(audio: np.ndarray) -> np.ndarray:
    """Ensure audio is 1D float32 array normalized to [-1.0, 1.0]."""
    arr = np.asarray(audio, dtype=np.float32)
    if arr.ndim > 1:
        arr = np.mean(arr, axis=-1).flatten()
    else:
        arr = arr.flatten()
    if len(arr) == 0:
        return arr
    max_val = np.max(np.abs(arr))
    if max_val > 1.0:
        arr = arr / max_val
    return arr


def _resample(audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
    """Resample 1D audio array using Fourier method."""
    if orig_sr == target_sr or len(audio) == 0:
        return audio
    num_target_samples = int(np.round(len(audio) * target_sr / orig_sr))
    if num_target_samples <= 0:
        return np.array([], dtype=np.float32)
    return scipy.signal.resample(audio, num_target_samples).astype(np.float32)


def _levinson_durbin(r: np.ndarray, order: int) -> tuple[np.ndarray, np.ndarray]:
    """Levinson-Durbin recursion for LPC coefficients and reflection coefficients."""
    a = np.zeros(order + 1, dtype=np.float64)
    k = np.zeros(order, dtype=np.float64)
    a[0] = 1.0
    
    e = float(r[0])
    if e <= 1e-12:
        return a[1:], k
        
    for i in range(1, order + 1):
        # Reflection coefficient
        sum_term = sum(a[j] * r[i - j] for j in range(1, i))
        ki = -(r[i] + sum_term) / e
        ki = np.clip(ki, -0.9999, 0.9999)
        k[i - 1] = ki
        
        # Update LPC coefficients
        a_new = a.copy()
        for j in range(1, i):
            a_new[j] = a[j] + ki * a[i - j]
        a_new[i] = ki
        a = a_new
        
        # Update prediction error energy
        e *= (1.0 - ki * ki)
        if e <= 1e-12:
            break
            
    return a[1:], k


def gsm_8k_telephony(audio: np.ndarray, sr: int = 16000) -> np.ndarray:
    """Simulate 8kHz GSM 06.10 Full Rate telephony codec.
    
    1. Resample to 8000 Hz.
    2. 4th-order Butterworth bandpass filter [300 Hz, 3400 Hz].
    3. LPC frame-by-frame (20ms) analysis and residual excitation quantization.
    4. LPC all-pole synthesis and 16kHz reconstruction.
    """
    orig_audio = _ensure_float32_mono(audio)
    orig_len = len(orig_audio)
    if orig_len == 0:
        return orig_audio

    # Minimum viable length: GSM frame is 20ms @ 8kHz = 160 samples.
    # After 2x downsampling, at least 2 samples are needed. Pad to 2 frames
    # (320 samples at 16kHz) so filter transients settle before we truncate back.
    MIN_LEN_16K = 320
    padded = False
    if orig_len < MIN_LEN_16K:
        orig_audio = np.pad(orig_audio, (0, MIN_LEN_16K - orig_len))
        padded = True

    # 1. Downsample to 8kHz
    audio_8k = _resample(orig_audio, sr, 8000)
    
    # 2. Telephony bandpass filter [300Hz, 3400Hz] at 8kHz (Nyquist = 4000Hz)
    sos = scipy.signal.butter(
        4, [300.0 / 4000.0, 3400.0 / 4000.0], btype="bandpass", output="sos"
    )
    filtered_8k = scipy.signal.sosfilt(sos, audio_8k).astype(np.float32)
    
    # 3. GSM LPC Frame-by-Frame Quantization (20ms = 160 samples @ 8kHz)
    frame_len = 160
    lpc_order = 8
    reconstructed_8k = np.zeros_like(filtered_8k)
    num_frames = int(np.ceil(len(filtered_8k) / frame_len))
    
    for f in range(num_frames):
        start = f * frame_len
        end = min(start + frame_len, len(filtered_8k))
        frame = filtered_8k[start:end]
        if len(frame) < lpc_order + 1:
            reconstructed_8k[start:end] = frame
            continue
            
        # Autocorrelation
        r = np.correlate(frame, frame, mode="full")
        r = r[len(frame) - 1 : len(frame) + lpc_order]
        
        # Levinson-Durbin
        lpc_coeffs, k_coeffs = _levinson_durbin(r, lpc_order)
        
        # Quantize reflection coefficients (Log Area Ratios LAR quantization)
        lar = np.log((1.0 + k_coeffs) / (1.0 - k_coeffs + 1e-12))
        lar_quant = np.round(lar * 8.0) / 8.0  # 8 quantization bins per LAR
        k_quant = (np.exp(lar_quant) - 1.0) / (np.exp(lar_quant) + 1.0)
        
        # Reconstruct quantized LPC coefficients from k_quant
        a_quant = np.zeros(lpc_order + 1, dtype=np.float64)
        a_quant[0] = 1.0
        for i in range(1, lpc_order + 1):
            ki = k_quant[i - 1]
            a_new = a_quant.copy()
            for j in range(1, i):
                a_new[j] = a_quant[j] + ki * a_quant[i - j]
            a_new[i] = ki
            a_quant = a_new
            
        # LPC Inverse filtering (Residual calculation)
        residual = scipy.signal.lfilter(a_quant, [1.0], frame)
        
        # Quantize residual excitation (RPE grid decimation simulation)
        # 4-bit uniform quantization on residual
        res_peak = np.max(np.abs(residual)) + 1e-12
        res_norm = residual / res_peak
        res_quant = np.round(res_norm * 7.0) / 7.0 * res_peak
        
        # LPC Synthesis filtering
        synth = scipy.signal.lfilter([1.0], a_quant, res_quant)
        
        # Copy to reconstructed buffer
        reconstructed_8k[start:end] = synth.astype(np.float32)
        
    # 4. Upsample back to 16kHz
    out_16k = _resample(reconstructed_8k, 8000, 16000)
    
    # Adjust length to match original (including undo of sub-frame padding)
    if len(out_16k) < orig_len:
        out_16k = np.pad(out_16k, (0, orig_len - len(out_16k)))
    elif len(out_16k) > orig_len:
        out_16k = out_16k[:orig_len]

    return np.clip(out_16k[:orig_len], -1.0, 1.0).astype(np.float32)


def g711_alaw(audio: np.ndarray, sr: int = 16000) -> np.ndarray:
    """Simulate ITU-T G.711 A-law logarithmic companding codec @ 8kHz 8-bit.
    
    A-law equation (A = 87.6):
    Compression:
      For |x| < 1/A:     y = sgn(x) * (A * |x|) / (1 + ln(A))
      For 1/A <= |x| <= 1: y = sgn(x) * (1 + ln(A * |x|)) / (1 + ln(A))
    Quantization:
      8-bit signed integer [-128, 127]
    Expansion:
      For |y| < 1 / (1 + ln(A)):     x = sgn(y) * (|y| * (1 + ln(A))) / A
      For 1/(1+ln(A)) <= |y| <= 1:   x = sgn(y) * exp(|y|*(1 + ln(A)) - 1) / A
    """
    orig_audio = _ensure_float32_mono(audio)
    orig_len = len(orig_audio)
    if orig_len == 0:
        return orig_audio
        
    # 1. Resample to 8kHz
    audio_8k = _resample(orig_audio, sr, 8000)
    
    # 2. A-law compression (A = 87.6)
    A = 87.6
    ln_A_plus_1 = 1.0 + np.log(A)
    
    x = np.clip(audio_8k, -1.0, 1.0)
    abs_x = np.abs(x)
    sgn_x = np.sign(x)
    
    y = np.zeros_like(x)
    mask_low = abs_x < (1.0 / A)
    mask_high = ~mask_low
    
    y[mask_low] = sgn_x[mask_low] * (A * abs_x[mask_low]) / ln_A_plus_1
    y[mask_high] = sgn_x[mask_high] * (1.0 + np.log(A * abs_x[mask_high])) / ln_A_plus_1
    
    # 3. 8-bit Quantization (256 discrete levels)
    y_int8 = np.clip(np.round(y * 128.0), -128.0, 127.0)
    y_dequant = y_int8 / 128.0
    
    # 4. A-law expansion (inverse companding)
    abs_y = np.abs(y_dequant)
    sgn_y = np.sign(y_dequant)
    
    x_rec = np.zeros_like(y_dequant)
    threshold_y = 1.0 / ln_A_plus_1
    mask_rec_low = abs_y < threshold_y
    mask_rec_high = ~mask_rec_low
    
    x_rec[mask_rec_low] = sgn_y[mask_rec_low] * (abs_y[mask_rec_low] * ln_A_plus_1) / A
    x_rec[mask_rec_high] = (
        sgn_y[mask_rec_high] * np.exp(abs_y[mask_rec_high] * ln_A_plus_1 - 1.0) / A
    )
    
    # 5. Upsample back to 16kHz
    out_16k = _resample(x_rec, 8000, 16000)
    
    # Match length
    if len(out_16k) < orig_len:
        out_16k = np.pad(out_16k, (0, orig_len - len(out_16k)))
    elif len(out_16k) > orig_len:
        out_16k = out_16k[:orig_len]
        
    return np.clip(out_16k, -1.0, 1.0).astype(np.float32)


def opus_compression(
    audio: np.ndarray, sr: int = 16000, bitrate_kbps: int = 24
) -> np.ndarray:
    """Simulate VoIP Opus codec (wideband/mediumband/narrowband) with psychoacoustic masking."""
    orig_audio = _ensure_float32_mono(audio)
    orig_len = len(orig_audio)
    if orig_len == 0:
        return orig_audio

    # STFT requires at least n_fft=320 samples. For sub-frame inputs zero-pad
    # to two full Opus frames (20ms each = 320 samples @ 16kHz) so the
    # STFT/ISTFT round-trip is well-defined, then truncate back to orig_len.
    N_FFT = 320
    if orig_len < N_FFT:
        orig_audio = np.pad(orig_audio, (0, N_FFT - orig_len))

    # 1. Resample to 16kHz if needed
    if sr != 16000:
        audio_16k = _resample(orig_audio, sr, 16000)
    else:
        audio_16k = orig_audio.copy()

    # 2. Bitrate-dependent cutoff frequency
    if bitrate_kbps >= 24:
        cutoff_hz = 7500.0
    elif bitrate_kbps >= 12:
        cutoff_hz = 5500.0
    else:
        cutoff_hz = 3600.0
        
    nyquist = 8000.0
    sos = scipy.signal.butter(
        4, min(cutoff_hz / nyquist, 0.95), btype="lowpass", output="sos"
    )
    band_filtered = scipy.signal.sosfilt(sos, audio_16k).astype(np.float32)
    
    # 3. STFT Psychoacoustic Sub-band Masking & Quantization (20ms frame = 320 samples)
    n_fft = 320
    hop_length = 160
    window = np.hanning(n_fft).astype(np.float32)
    
    # STFT
    f, t, Zxx = scipy.signal.stft(
        band_filtered, fs=16000, window=window, nperseg=n_fft, noverlap=hop_length
    )
    
    mag = np.abs(Zxx)
    phase = np.angle(Zxx)
    
    bark_edges = [
        0, 100, 200, 300, 400, 510, 630, 770, 920, 1080,
        1270, 1480, 1720, 2000, 2320, 2700, 3150, 3700, 4400, 5300, 6400, 7700
    ]
    
    freq_bins = f
    mag_quant = mag.copy()
    
    q_levels = max(8, int(bitrate_kbps * 2))
    
    for b in range(len(bark_edges) - 1):
        low_f, high_f = bark_edges[b], bark_edges[b + 1]
        bin_mask = (freq_bins >= low_f) & (freq_bins < high_f)
        if not np.any(bin_mask):
            continue
            
        band_energy = np.mean(mag[bin_mask, :] ** 2, axis=0, keepdims=True)
        masking_threshold = band_energy * 0.05  # 13 dB masking threshold
        
        band_mag = mag[bin_mask, :]
        band_max = np.max(band_mag, axis=0, keepdims=True) + 1e-12
        band_norm = band_mag / band_max
        
        band_q = np.round(band_norm * q_levels) / q_levels * band_max
        
        below_mask = band_mag < np.sqrt(masking_threshold)
        band_q[below_mask] *= 0.85
        
        mag_quant[bin_mask, :] = band_q
        
    # Reconstruct ISTFT
    Zxx_recon = mag_quant * np.exp(1j * phase)
    _, out_reconstructed = scipy.signal.istft(
        Zxx_recon, fs=16000, window=window, nperseg=n_fft, noverlap=hop_length
    )
    
    # Adjust length — always truncate to orig_len to undo any sub-frame padding.
    if len(out_reconstructed) < orig_len:
        out_reconstructed = np.pad(
            out_reconstructed, (0, orig_len - len(out_reconstructed))
        )

    return np.clip(out_reconstructed[:orig_len], -1.0, 1.0).astype(np.float32)


def apply_codec(audio: np.ndarray, codec_name: str, sr: int = 16000) -> np.ndarray:
    """Apply a named codec degradation transformation to audio array."""
    token = codec_name.strip().lower()
    
    if token in ("clean", "clean_16k", "pcm16", "none"):
        return _ensure_float32_mono(audio)
    elif token in ("gsm_8k", "gsm_8k_telephony", "gsm", "telephony"):
        return gsm_8k_telephony(audio, sr)
    elif token in ("g711_alaw", "g711", "alaw"):
        return g711_alaw(audio, sr)
    elif token in ("opus_24k", "opus", "voip"):
        return opus_compression(audio, sr, bitrate_kbps=24)
    elif token == "opus_12k":
        return opus_compression(audio, sr, bitrate_kbps=12)
    elif token in ("opus_8k", "opus_nb"):
        return opus_compression(audio, sr, bitrate_kbps=8)
    elif "_then_" in token or "->" in token:
        chain = token.replace("->", "_then_").split("_then_")
        return apply_codec_chain(audio, chain, sr)
    else:
        raise ValueError(f"Unknown codec transformation: {codec_name!r}")


def apply_codec_chain(
    audio: np.ndarray, chain: Sequence[str] | str, sr: int = 16000
) -> np.ndarray:
    """Apply a multi-hop sequence of codecs in order (e.g. ['opus_24k', 'g711_alaw'])."""
    if isinstance(chain, str):
        steps = [s.strip() for s in chain.replace("->", "_then_").split("_then_") if s.strip()]
    else:
        steps = list(chain)
        
    current_audio = _ensure_float32_mono(audio)
    for step in steps:
        current_audio = apply_codec(current_audio, step, sr)
        
    return current_audio


def get_available_codecs() -> list[str]:
    """Return list of standard supported codec identifiers."""
    return [
        "clean_16k",
        "gsm_8k",
        "g711_alaw",
        "opus_24k",
        "opus_12k",
        "opus_24k_then_g711",
    ]
