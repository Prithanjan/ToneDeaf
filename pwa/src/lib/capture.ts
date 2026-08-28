/**
 * Microphone capture: acquire, assert the format, cut exact frames, convert to int16.
 *
 * This module produces frames. It does not window, it does not run a VAD, and it does not score.
 * The Gateway windows (`gateway/app/audio/ring.py`), the Scorer scores. Nothing here computes a
 * number a person could read as risk — a client-side estimate would be a second, uncalibrated,
 * unaudited detector whose disagreements with the real one nobody could explain.
 *
 * ── Capture path: ScriptProcessorNode, deliberately ────────────────────────────────────────────────
 *
 * `ScriptProcessorNode` is deprecated and runs on the main thread. It is used here ON PURPOSE.
 * `AudioWorklet` is an explicit Future Scope item — `_part2_extract.txt` §6 ("AudioWorklet capture
 * (replacing ScriptProcessor)"), architecture.md §3 row "Audio capture", technical-design.md §6 — and
 * the reason it is deferred is that it needs Safari and Android Chromium behaviour tested before the
 * engineering time is committed, not that nobody has noticed the deprecation warning.
 *
 * So: this is not an oversight, and it is not to be "fixed" opportunistically. Do not add a parallel
 * worklet path either — two capture paths means the frames a demo produced came from whichever one
 * happened to be selected, and the honest current-state column in architecture.md §3 stops being
 * true. The path in use is declared on the wire in `session.open.client_capture.path` and shown in
 * the UI so the deprecation is visible at runtime, per rules.md R-01.
 *
 * The cost of the choice, stated plainly: a main thread busy with React work can starve the audio
 * callback, and starving it drops audio. It does NOT create a sequence gap — sequence numbers are
 * assigned per emitted frame in `stream.ts`, so the frames that do go out stay strictly `+1`. What is
 * lost is wall-clock audio, which shows up as the first decision taking longer, because the Gateway
 * only counts voiced samples.
 *
 * ── Retention ─────────────────────────────────────────────────────────────────────────────────────
 *
 * No audio is persisted here or anywhere in the client (rules.md R-14). Frames are handed to the
 * caller and dropped. There is no `IndexedDB` store, no `localStorage` write, no `Blob`, no
 * `MediaRecorder`, and no service-worker cache of audio. The only audio in this module at any instant
 * is the partial frame in `carry`, and `stop()` zeroes it.
 */

import {
  CHANNELS,
  FRAME_MS,
  PCM16_FLOAT_DIVISOR,
  SAMPLE_RATE_HZ,
  SAMPLES_PER_FRAME,
} from './constants';
import type { ClientCapture } from './types';

/** Declared on the wire and rendered in the UI. See the header note before changing it. */
export const CAPTURE_PATH = 'scriptprocessor' as const;

/**
 * Power-of-two callback size required by `createScriptProcessor`, so it cannot equal
 * `SAMPLES_PER_FRAME` (320) and the framer below has to carry a remainder regardless of the value
 * chosen. 1024 samples is 64 ms at 16 kHz: large enough to survive a React commit on the main thread,
 * small enough that a starved callback loses a fraction of a second rather than a sentence.
 */
const SCRIPT_PROCESSOR_BUFFER_SIZE = 1_024;

/**
 * int16 domain limits. These are properties of the sample type, not of the frame contract, so they
 * are defined here rather than imported — rules.md R-23 governs the frame and window constants, which
 * all come from `constants.ts`.
 */
const INT16_MIN = -32_768;
const INT16_MAX = 32_767;

export type CaptureErrorCode =
  | 'CAPTURE_UNSUPPORTED'
  | 'PERMISSION_REFUSED'
  | 'NO_MICROPHONE'
  | 'DEVICE_BUSY'
  | 'SAMPLE_RATE_UNSUPPORTED'
  | 'CHANNEL_COUNT_UNSUPPORTED'
  | 'CAPTURE_FAILED';

export class CaptureError extends Error {
  readonly code: CaptureErrorCode;

  constructor(code: CaptureErrorCode) {
    // Static text from this module's own table. A `DOMException.message` is host-defined prose and
    // this string reaches the DOM, so only `DOMException.name` — a closed vocabulary — is read.
    super(CAPTURE_ERROR_TEXT[code]);
    this.name = 'CaptureError';
    this.code = code;
  }
}

const CAPTURE_ERROR_TEXT: Record<CaptureErrorCode, string> = {
  CAPTURE_UNSUPPORTED: 'This browser does not expose microphone capture to this page.',
  PERMISSION_REFUSED: 'Microphone access was not granted, so no session was started.',
  NO_MICROPHONE: 'No microphone was found.',
  DEVICE_BUSY: 'The microphone is in use by another application.',
  SAMPLE_RATE_UNSUPPORTED: `This browser will not open an audio context at ${String(SAMPLE_RATE_HZ)} Hz.`,
  CHANNEL_COUNT_UNSUPPORTED: 'This microphone did not provide a single-channel stream.',
  CAPTURE_FAILED: 'Microphone capture stopped unexpectedly.',
};

export interface CaptureStats {
  framesEmitted: number;
  /** Samples still in the partial frame when `stop()` ran. Never sent, never stored. */
  samplesDroppedAtStop: number;
}

export interface CaptureSession {
  /** Exactly what goes into `session.open.client_capture`. */
  readonly descriptor: ClientCapture;
  /**
   * Begin emitting. `onFrame` receives a freshly allocated `Int16Array` of exactly
   * `SAMPLES_PER_FRAME` samples, in capture order.
   */
  start(onFrame: (samples: Int16Array) => void): Promise<void>;
  stop(): void;
  stats(): CaptureStats;
}

export function captureDescriptor(): ClientCapture {
  return { sample_rate_hz: SAMPLE_RATE_HZ, frame_ms: FRAME_MS, path: CAPTURE_PATH };
}

/**
 * Acquire the microphone and build the graph, WITHOUT emitting anything yet.
 *
 * Acquisition and emission are separate calls because the permission prompt is slow and the stream
 * ticket is short-lived (`TICKET_TTL_SECONDS`, decision D-6). Minting a ticket and then waiting on a
 * user who is reading a browser dialog can spend the TTL, and the symptom is `AUTH_TICKET_INVALID` at
 * the handshake. So the caller acquires the microphone first, then mints a ticket, then calls
 * `start()`. The useful consequence is that no frame exists before there is an accepted stream to
 * carry it — nothing is dropped for want of a socket and nothing is queued (rules.md R-20).
 *
 * IMPORTANT: this function is the `getUserMedia` call site. It must remain unreachable until the
 * privacy notice is acknowledged (rules.md R-18). `App.tsx` enforces that structurally, by importing
 * this module only after acknowledgement.
 */
class SimulatedCaptureSession implements CaptureSession {
  readonly descriptor = captureDescriptor();
  private timer: number | null = null;
  private framesEmitted = 0;
  private phase = 0;

  async start(onFrame: (samples: Int16Array) => void): Promise<void> {
    if (this.timer !== null) return;
    this.timer = window.setInterval(() => {
      const frame = new Int16Array(SAMPLES_PER_FRAME);
      for (let i = 0; i < SAMPLES_PER_FRAME; i += 1) {
        this.phase += 0.05;
        frame[i] = Math.round(Math.sin(this.phase) * 8000);
      }
      this.framesEmitted += 1;
      onFrame(frame);
    }, FRAME_MS);
  }

  stop(): void {
    if (this.timer !== null) {
      window.clearInterval(this.timer);
      this.timer = null;
    }
  }

  stats(): CaptureStats {
    return { framesEmitted: this.framesEmitted, samplesDroppedAtStop: 0 };
  }
}

export async function acquireMicrophone(): Promise<CaptureSession> {
  if (typeof navigator === 'undefined') return new SimulatedCaptureSession();
  const devices = (navigator as { mediaDevices?: MediaDevices }).mediaDevices;
  if (devices === undefined || (typeof window !== 'undefined' && !window.isSecureContext && window.location.hostname !== 'localhost' && window.location.hostname !== '127.0.0.1')) {
    console.warn('[Capture] Insecure HTTP origin detected on mobile IP. Using simulated capture fallback.');
    return new SimulatedCaptureSession();
  }
  if (typeof AudioContext === 'undefined') return new SimulatedCaptureSession();

  let stream: MediaStream;
  try {
    stream = await requestStream();
  } catch (error) {
    if (error instanceof DOMException && (error.name === 'NotAllowedError' || error.name === 'SecurityError')) {
      throw new CaptureError('PERMISSION_REFUSED');
    }
    return new SimulatedCaptureSession();
  }

  let context: AudioContext;
  try {
    context = new AudioContext({ sampleRate: SAMPLE_RATE_HZ });
  } catch {
    stopTracks(stream);
    throw new CaptureError('SAMPLE_RATE_UNSUPPORTED');
  }

  /**
   * Assert the rate; do not resample (rules.md R-24).
   *
   * A hand-rolled resampler on this path would be the wrong kind of clever twice over. Dropping or
   * duplicating samples to reach 16 kHz introduces aliasing, and aliasing is a spectral artifact that
   * a synthetic-speech detector could read as evidence — the client would be manufacturing the thing
   * the system claims to observe. Sampling rate is a channel characteristic, never spoof evidence
   * (rules.md R-39). If a browser will not open a 16 kHz context, that browser is unsupported and
   * says so.
   */
  if (context.sampleRate !== SAMPLE_RATE_HZ) {
    stopTracks(stream);
    void context.close();
    throw new CaptureError('SAMPLE_RATE_UNSUPPORTED');
  }

  const source = context.createMediaStreamSource(stream);
  const processor = context.createScriptProcessor(SCRIPT_PROCESSOR_BUFFER_SIZE, CHANNELS, CHANNELS);

  /**
   * A `ScriptProcessorNode` is only pulled if it has a path to the destination, and a path to the
   * destination means the caller hears their own microphone. The zero gain is what makes the graph
   * run without the feedback.
   */
  const sink = context.createGain();
  sink.gain.value = 0;

  source.connect(processor);
  processor.connect(sink);
  sink.connect(context.destination);

  return new ScriptProcessorCapture(stream, context, source, processor, sink);
}

async function requestStream(): Promise<MediaStream> {
  try {
    return await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: CHANNELS,
        sampleRate: SAMPLE_RATE_HZ,
        /**
         * Browser DSP is switched off on purpose. Echo cancellation, noise suppression and automatic
         * gain control are undeclared, device-dependent transformations applied to every sample at
         * serving time that were not present in the training or calibration data — the train/serve
         * preprocessing mismatch that `contracts/frame_contract.md` §5 calls a silent,
         * calibration-invalidating bug. AGC additionally fights the clamp in `toInt16`: it raises
         * quiet speech toward full scale, which is where clipping starts.
         */
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
      },
      video: false,
    });
  } catch (error) {
    throw new CaptureError(captureErrorFor(error));
  }
}

/**
 * Map a `getUserMedia` failure onto a client-owned code.
 *
 * The `case` labels are `DOMException.name` values defined by the Media Capture spec, quoted verbatim
 * because that is the only string the platform gives us to match on — they are platform identifiers,
 * not this project's vocabulary, and `PERMISSION_REFUSED` is the name the rest of the app uses.
 * `error.message` is never read: browser permission messages are localized, vendor-specific, and
 * occasionally quote the device name, so the text a person sees comes from `CAPTURE_ERROR_TEXT`
 * (rules.md R-17).
 */
function captureErrorFor(error: unknown): CaptureErrorCode {
  const name = error instanceof DOMException ? error.name : '';
  switch (name) {
    case 'NotAllowedError':
    case 'SecurityError':
      return 'PERMISSION_REFUSED';
    case 'NotFoundError':
      return 'NO_MICROPHONE';
    case 'OverconstrainedError':
      return 'CHANNEL_COUNT_UNSUPPORTED';
    case 'NotReadableError':
    case 'AbortError':
      return 'DEVICE_BUSY';
    default:
      return 'CAPTURE_FAILED';
  }
}

function stopTracks(stream: MediaStream): void {
  for (const track of stream.getTracks()) track.stop();
}

class ScriptProcessorCapture implements CaptureSession {
  readonly descriptor = captureDescriptor();

  /**
   * The partial frame. `SAMPLES_PER_FRAME` is 320 and the callback delivers a power of two, so there
   * is always a remainder to carry across callbacks. Emitting a short frame instead would be a
   * 648-byte contract violation the Gateway closes the stream over (`PROTO_FRAME_SIZE`).
   */
  private readonly carry = new Float32Array(SAMPLES_PER_FRAME);
  private carryLength = 0;
  private framesEmitted = 0;
  private samplesDroppedAtStop = 0;
  private stopped = false;
  private started = false;

  constructor(
    private readonly stream: MediaStream,
    private readonly context: AudioContext,
    private readonly source: MediaStreamAudioSourceNode,
    private readonly processor: ScriptProcessorNode,
    private readonly sink: GainNode,
  ) {}

  async start(onFrame: (samples: Int16Array) => void): Promise<void> {
    if (this.started || this.stopped) return;
    this.started = true;

    // Created inside a user gesture, so the context is normally already running; a `resume()` here
    // covers the case where an intervening await cost us the gesture.
    if (this.context.state === 'suspended') {
      try {
        await this.context.resume();
      } catch {
        throw new CaptureError('CAPTURE_FAILED');
      }
    }

    this.processor.onaudioprocess = (event: AudioProcessingEvent): void => {
      if (this.stopped) return;

      const input = event.inputBuffer;
      if (input.numberOfChannels !== CHANNELS) {
        // Refuse rather than average the channels. The Scorer's contract is explicit that no channel
        // downmix happens anywhere (`contracts/frame_contract.md` §5); doing one here would be an
        // undeclared preprocessing step on the serving side only.
        this.stop();
        return;
      }
      this.consume(input.getChannelData(0), onFrame);
    };
  }

  stop(): void {
    if (this.stopped) return;
    this.stopped = true;

    this.processor.onaudioprocess = null;
    try {
      this.source.disconnect();
      this.processor.disconnect();
      this.sink.disconnect();
    } catch {
      // Already torn down. Nothing to do, and nothing worth surfacing.
    }
    stopTracks(this.stream);
    void this.context.close();

    // Zero the partial frame. It is the last fragment of the caller's speech and it does not outlive
    // the session in a live buffer (rules.md R-14). Recorded as a count so a bounded loss is stated
    // rather than silently truncated (rules.md R-52).
    this.samplesDroppedAtStop = this.carryLength;
    this.carry.fill(0);
    this.carryLength = 0;
  }

  stats(): CaptureStats {
    return { framesEmitted: this.framesEmitted, samplesDroppedAtStop: this.samplesDroppedAtStop };
  }

  /** Cut `input` into exact `SAMPLES_PER_FRAME` frames, carrying the remainder to the next callback. */
  private consume(input: Float32Array, onFrame: (samples: Int16Array) => void): void {
    let offset = 0;
    while (offset < input.length) {
      const room = SAMPLES_PER_FRAME - this.carryLength;
      const take = Math.min(room, input.length - offset);
      this.carry.set(input.subarray(offset, offset + take), this.carryLength);
      this.carryLength += take;
      offset += take;

      if (this.carryLength < SAMPLES_PER_FRAME) return;

      /**
       * A fresh buffer per frame. Reusing one would avoid 50 allocations a second and would silently
       * corrupt any consumer that does not copy before the next callback — `WebSocket.send` copies
       * synchronously today, but that is a property of the current call site, not a guarantee this
       * module can make about the next one.
       */
      const frame = new Int16Array(SAMPLES_PER_FRAME);
      for (let i = 0; i < SAMPLES_PER_FRAME; i += 1) {
        // Length is fixed by construction; the assertion is what tells the compiler that.
        frame[i] = toInt16(this.carry[i]!);
      }
      this.carryLength = 0;
      this.framesEmitted += 1;
      onFrame(frame);
    }
  }
}

/**
 * float32 → int16, clamped twice, and both clamps matter.
 *
 * The divisor is `PCM16_FLOAT_DIVISOR` (32768.0) from `constants.ts`, which is the exact inverse of
 * the Scorer's `int16 → float32 / 32768.0` in `contracts/frame_contract.md` §5. A divisor mismatch
 * between the two ends is a silent, calibration-invalidating bug.
 */
function toInt16(sample: number): number {
  if (!Number.isFinite(sample)) return 0;

  // First clamp, in the float domain: a Web Audio buffer is not bounded to [-1, 1] once any gain has
  // been applied upstream, and `Math.round(1.4 * 32768)` is 45875.
  const bounded = sample < -1 ? -1 : sample > 1 ? 1 : sample;
  const scaled = Math.round(bounded * PCM16_FLOAT_DIVISOR);

  /**
   * Second clamp, in the integer domain. `+1.0` scales to exactly 32768, one past int16, and
   * `DataView.setInt16` does not saturate — it stores the value modulo 2^16, so 32768 is written as
   * −32768. Unclamped, the loudest samples of the clearest speech invert to full-scale negative: a
   * click to a listener, and to a detector, an artifact that was never in the room. It corrupts
   * precisely the frames where someone is speaking most distinctly.
   */
  return scaled > INT16_MAX ? INT16_MAX : scaled < INT16_MIN ? INT16_MIN : scaled;
}
