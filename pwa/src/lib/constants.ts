/**
 * Frame, window, and protocol constants — the CLIENT half of the mirror.
 *
 * MIRRORS `gateway/app/constants.py`. That file is the source of truth; this one exists because the
 * browser cannot import Python. `gateway/tests/test_constants_parity.py` parses BOTH files and asserts
 * every shared name has an equal value, so a change here without the matching change there fails CI.
 *
 * A frame-size divergence between client and server is the most likely silent integration failure in
 * this build: the server rejects every frame, the client sees a close code, and the obvious diagnosis
 * ("the WebSocket is broken") is wrong. Hence the parity test rather than a comment asking people to
 * remember.
 *
 * Normative source: `contracts/frame_contract.md` (contract id `raw-waveform-v1`).
 * Never inline 648, 640, 81920, or 40960 anywhere else (rules.md R-23).
 */

// --- Audio format (decision D-1: PCM is int16 LITTLE-endian) --------------------------------------
export const CONTRACT_ID = 'raw-waveform-v1';
export const SAMPLE_RATE_HZ = 16_000;
export const CHANNELS = 1;

// --- Frame ---------------------------------------------------------------------------------------
export const FRAME_MS = 20;
export const SAMPLES_PER_FRAME = 320; // 16000 * 0.020
export const BYTES_PER_FRAME_PAYLOAD = 640; // 320 * 2

/**
 * Decision D-2: the sequence header is uint64 BIG-endian (network order), while the payload is
 * int16 LITTLE-endian. They deliberately disagree.
 *
 * In practice that means, for a DataView `view`:
 *
 *     view.setBigUint64(0, seq);                  // no third argument -> big-endian (the default)
 *     view.setInt16(8 + i * 2, sample, true);     // true -> little-endian
 *
 * Getting these the same way round is the bug this comment exists to prevent: a little-endian header
 * turns frame 1 into sequence 72057594037927936, and the server closes the stream on frame 2.
 */
export const SEQ_PREFIX_BYTES = 8;
export const SEQ_IS_BIG_ENDIAN = true;
export const PCM_IS_LITTLE_ENDIAN = true;

/** Decision D-3: every binary WebSocket frame is EXACTLY this many bytes. Never padded (R-24). */
export const WS_FRAME_BYTES = 648; // 8 + 640

// --- Analysis window -----------------------------------------------------------------------------
// The client does not window audio — the Gateway does. These are here so the UI can explain the
// cadence honestly: the first decision needs 2.56 s of VOICED audio, which is more than 2.56 s of
// wall clock, and a progress indicator that implies otherwise is a false promise.
export const WINDOW_MS = 2_560;
export const WINDOW_SAMPLES = 40_960; // 16000 * 2.560
export const WINDOW_BYTES = 81_920; // 40960 * 2

export const HOP_MS = 640;
export const HOP_SAMPLES = 10_240; // 16000 * 0.640
export const FRAMES_PER_HOP = 32; // 10240 / 320
export const HOPS_PER_WINDOW = 4; // 2560 / 640 -> 75% overlap

// --- Model input ---------------------------------------------------------------------------------
// Unused by the client. Present so the parity test covers the value: a divisor mismatch between
// training and serving preprocessing is a silent, calibration-invalidating bug.
export const PCM16_FLOAT_DIVISOR = 32_768.0;

// --- Protocol guards -----------------------------------------------------------------------------
export const MAX_TEXT_FRAME_BYTES = 4_096;
export const TICKET_TTL_SECONDS = 60;
export const WS_SUBPROTOCOL = 'sih-v1';
export const WS_TICKET_SUBPROTOCOL_PREFIX = 'sih-ticket.';

/**
 * Arithmetic identities, asserted at module load exactly as `constants.py::_self_check` does.
 *
 * Throwing at import in a browser is deliberate: if these are wrong, every buffer size downstream is
 * wrong, and a blank screen with one loud console error is a better failure than a session that
 * streams malformed frames and blames the network.
 */
function selfCheck(): void {
  const identities: Array<[string, boolean]> = [
    ['SAMPLES_PER_FRAME', SAMPLES_PER_FRAME === (SAMPLE_RATE_HZ * FRAME_MS) / 1000],
    ['BYTES_PER_FRAME_PAYLOAD', BYTES_PER_FRAME_PAYLOAD === SAMPLES_PER_FRAME * 2],
    ['WS_FRAME_BYTES', WS_FRAME_BYTES === SEQ_PREFIX_BYTES + BYTES_PER_FRAME_PAYLOAD],
    ['WINDOW_SAMPLES', WINDOW_SAMPLES === (SAMPLE_RATE_HZ * WINDOW_MS) / 1000],
    ['WINDOW_BYTES', WINDOW_BYTES === WINDOW_SAMPLES * 2],
    ['HOP_SAMPLES', HOP_SAMPLES === (SAMPLE_RATE_HZ * HOP_MS) / 1000],
    ['FRAMES_PER_HOP', FRAMES_PER_HOP === HOP_SAMPLES / SAMPLES_PER_FRAME],
    ['HOPS_PER_WINDOW', HOPS_PER_WINDOW === WINDOW_MS / HOP_MS],
    ['window is a whole number of hops', WINDOW_SAMPLES % HOP_SAMPLES === 0],
  ];
  const broken = identities.filter(([, ok]) => !ok).map(([name]) => name);
  if (broken.length > 0) {
    throw new Error(`constants.ts self-check failed: ${broken.join(', ')}`);
  }
}

selfCheck();
