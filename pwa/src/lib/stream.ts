/**
 * WebSocket client: handshake, framing, sequencing, reconnect.
 *
 * ── The two byte orders disagree, on purpose ───────────────────────────────────────────────────────
 *
 * Decision D-2 / rules.md R-25: the sequence header is `uint64` BIG-endian and the PCM payload is
 * `int16` LITTLE-endian. `encodeFrame` is the one place in the client that writes either, and the
 * asymmetry is visible on two adjacent lines so it cannot be "tidied" into consistency:
 *
 *     view.setBigUint64(0, seq);                              // no third argument -> big-endian
 *     view.setInt16(offset, sample, PCM_IS_LITTLE_ENDIAN);    // true -> little-endian
 *
 * Writing the header little-endian sends frame 1 as sequence 72057594037927936. The server accepts it
 * (it is the first frame, so there is nothing to compare against), then closes on frame 2 with
 * `PROTO_SEQUENCE` — because 144115188075855872 is not 72057594037927937. The visible symptom is a
 * stream that dies two frames in with an error naming the wrong field.
 *
 * ── `encodeFrame` is pure ──────────────────────────────────────────────────────────────────────────
 *
 * It is the PWA counterpart of `gateway/app/ws/frames.py` and is held to the same standard
 * (rules.md R-53): no clock, no socket, no module state. `backoffDelayMs` takes its randomness as an
 * argument for the same reason. Everything impure lives in `Stream` below.
 */

import {
  BYTES_PER_FRAME_PAYLOAD,
  FRAMES_PER_HOP,
  HOPS_PER_WINDOW,
  MAX_TEXT_FRAME_BYTES,
  PCM_IS_LITTLE_ENDIAN,
  SAMPLES_PER_FRAME,
  SEQ_IS_BIG_ENDIAN,
  SEQ_PREFIX_BYTES,
  WS_FRAME_BYTES,
  WS_SUBPROTOCOL,
  WS_TICKET_SUBPROTOCOL_PREFIX,
} from './constants';
import { parseServerEvent } from './types';
import type { ServerEvent, SessionOpen, StreamTicketResponse, WsErrorCode } from './types';

/** Derived, not written: 640 / 320. `DataView` needs a byte stride and this is where it comes from. */
const BYTES_PER_SAMPLE = BYTES_PER_FRAME_PAYLOAD / SAMPLES_PER_FRAME;

/**
 * The endianness flags in `constants.ts` are mirrored by the parity test, so they are the thing a
 * future contract change would flip. This module hard-codes the two `DataView` argument forms, so a
 * flipped flag here would be silently ignored — asserted at load instead (rules.md R-25).
 */
/* eslint-disable-next-line @typescript-eslint/no-unnecessary-condition --
   statically dead today, which is the point: both flags are literal `true`, so this costs nothing until
   the day someone flips one in `constants.ts`, and then it is a loud failure at import instead of a
   stream the Gateway closes on frame 2 with `PROTO_SEQUENCE`. */
if (!SEQ_IS_BIG_ENDIAN || !PCM_IS_LITTLE_ENDIAN) {
  throw new Error('stream.ts encodes seq big-endian and PCM little-endian; constants.ts now disagrees');
}

export class FrameError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'FrameError';
  }
}

/**
 * One binary frame: `WS_FRAME_BYTES` exactly, never padded and never trimmed (rules.md R-24).
 *
 * A short final frame is not a smaller frame, it is a `PROTO_FRAME_SIZE` close. Callers hand over
 * complete frames or nothing; `capture.ts` carries the remainder rather than flushing a stub.
 */
export function encodeFrame(seq: bigint, samples: Int16Array): ArrayBuffer {
  if (samples.length !== SAMPLES_PER_FRAME) {
    throw new FrameError(`frame must be ${String(SAMPLES_PER_FRAME)} samples, got ${String(samples.length)}`);
  }
  if (seq < 0n) throw new FrameError('sequence must not be negative');

  const buffer = new ArrayBuffer(WS_FRAME_BYTES);
  const view = new DataView(buffer);

  // Big-endian: `setBigUint64` treats a missing third argument as big-endian. Do not add `true` here.
  view.setBigUint64(0, seq);

  // Little-endian, one sample at a time. `new Int16Array(buffer, SEQ_PREFIX_BYTES, SAMPLES_PER_FRAME)`
  // would be faster and would encode host order — little-endian on every machine anyone will run this
  // on, and big-endian on the one that eventually breaks it. The explicit flag costs 320 calls per
  // 20 ms and removes the platform assumption.
  for (let i = 0; i < SAMPLES_PER_FRAME; i += 1) {
    view.setInt16(SEQ_PREFIX_BYTES + i * BYTES_PER_SAMPLE, samples[i]!, PCM_IS_LITTLE_ENDIAN);
  }
  return buffer;
}

// --- Reconnect policy ----------------------------------------------------------------------------

const BACKOFF_BASE_MS = 500;
const BACKOFF_CAP_MS = 8_000;
const MAX_RECONNECT_ATTEMPTS = 5;
const HANDSHAKE_TIMEOUT_MS = 10_000;

/**
 * Exponential backoff with FULL jitter: uniform over `[0, min(cap, base · 2^attempt))`.
 *
 * Full jitter rather than the more common `delay/2 + random(delay/2)` because the case this has to
 * survive is a Caddy config reload, which closes every open WebSocket at the same instant
 * (rules.md R-35). Correlated clients with the same partly-deterministic delay reconnect in a
 * thundering herd and get refused for capacity — `BACKPRESSURE_REJECT` — which looks like a Gateway
 * fault rather than a retry-storm the client caused.
 *
 * `random` is a parameter so the schedule is testable without stubbing globals (rules.md R-53).
 */
export function backoffDelayMs(attempt: number, random: () => number): number {
  const ceiling = Math.min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * 2 ** Math.max(0, attempt));
  return Math.floor(random() * ceiling);
}

/**
 * Close codes worth retrying: the connection died for a reason that may not be true a second later.
 *
 * 1001 peer going away · 1006 abnormal, no close frame (a Caddy reload, a dropped Wi-Fi association)
 * 1011 `SCORER_UNAVAILABLE` · 1012 service restart · 1013 `BACKPRESSURE_REJECT`, i.e. try later.
 *
 * Everything else is deterministic and reconnecting reproduces it: 1003 is the `PROTO_*` family
 * (frame size, sequence, first message), 1008 covers the `AUTH_*` family plus
 * `PROTO_PURPOSE_MISMATCH` and `SESSION_ALREADY_STREAMING`, 1009 is a text frame over
 * `MAX_TEXT_FRAME_BYTES`. Retrying a bad ticket four times turns one authentication failure into
 * four, and buries the first.
 */
const RETRYABLE_CLOSE_CODES: readonly number[] = [1001, 1006, 1011, 1012, 1013];

/** `WebSocket.close` accepts 1000 or 3000–4999 from a browser; 1001 throws `InvalidAccessError`. */
const CLOSE_NORMAL = 1000;
/** Private range. Client abandoned the stream because its own send buffer was over the hard bound. */
const CLOSE_CLIENT_BACKPRESSURE = 4000;

/**
 * Send-buffer bounds, in frames, converted to bytes through `WS_FRAME_BYTES`.
 *
 * Soft bound — one hop (`FRAMES_PER_HOP`, 640 ms): above this, new frames are DROPPED and counted.
 * Not queued. Queued audio is retained audio (rules.md R-20), and a client-side backlog is retention
 * the privacy notice never mentioned.
 *
 * Hard bound — one window (`FRAMES_PER_HOP · HOPS_PER_WINDOW`, 2.56 s): above this the oldest frame
 * still waiting is older than the window currently being scored, so the stream has stopped being a
 * live measurement. Abandon and reconnect, which resets the sequence and starts a clean stream rather
 * than splicing across a 2.5-second hole.
 *
 * Dropping frames is honest but not free: each drop splices the waveform, and the Gateway may see it
 * as `PACKET_LOSS_SUSPECTED`. That is the right outcome — it is a quality flag in the schema, so a
 * degraded channel reduces eligible windows instead of quietly producing evidence (rules.md R-09).
 */
const SEND_BUFFER_SOFT_BYTES = FRAMES_PER_HOP * WS_FRAME_BYTES;
const SEND_BUFFER_HARD_BYTES = FRAMES_PER_HOP * HOPS_PER_WINDOW * WS_FRAME_BYTES;

// --- Client-owned status text --------------------------------------------------------------------

/**
 * The client's own copy of the error vocabulary, keyed by the wire `code`.
 *
 * The server also sends a `message`; it is never read (rules.md R-17). The Gateway guarantees static
 * text, but a client that renders whatever arrived makes that guarantee the only thing between a
 * future logging change and a caller reference on screen. `Record<WsErrorCode, string>` means a new
 * code added to `contracts/openapi.yaml` fails the type-check here instead of rendering blank.
 */
export const WS_ERROR_TEXT: Record<WsErrorCode, string> = {
  AUTH_TICKET_MISSING: 'The stream ticket was not offered. No audio was sent.',
  AUTH_TICKET_INVALID: 'The stream ticket was expired, already spent, or for another session.',
  AUTH_ORIGIN_DENIED: 'The Gateway does not accept streams from this site address.',
  PROTO_FRAME_SIZE: 'The Gateway refused a frame as the wrong size. This is a client bug, not a network fault.',
  PROTO_SEQUENCE: 'The Gateway saw a gap or a repeat in the frame sequence. This is a client bug.',
  PROTO_FIRST_MESSAGE: 'The Gateway refused the session opening message.',
  PROTO_PURPOSE_MISMATCH: 'The purpose recorded for this session does not match the one sent on the stream.',
  PROTO_PAYLOAD_TOO_LARGE: 'The session opening message was too large.',
  SESSION_ALREADY_STREAMING: 'This session already has a live stream open elsewhere.',
  BACKPRESSURE_REJECT: 'The Gateway is at capacity and refused the stream rather than hold audio in a queue.',
  SCORER_UNAVAILABLE: 'The scoring service did not answer. No risk was estimated for this stretch of audio.',
};

const STATUS_TEXT = {
  handshakeTimeout: 'The Gateway did not complete the stream handshake in time.',
  subprotocolRefused: 'The Gateway did not agree to the expected stream protocol version.',
  ticketUnavailable: 'A stream ticket could not be obtained.',
  transportLost: 'The stream connection dropped.',
  retriesExhausted: 'The stream could not be re-established. Capture has stopped.',
  clientBackpressure: 'The connection could not keep up with capture, so the stream was restarted.',
  stoppedByOperator: 'Streaming stopped.',
} as const;

// --- Public surface ------------------------------------------------------------------------------

export type StreamPhase = 'connecting' | 'streaming' | 'reconnecting' | 'closed';

export interface StreamStatus {
  phase: StreamPhase;
  /** Reconnect attempts consumed. `0` on the first connection. */
  attempt: number;
  framesSent: number;
  /** Frames capture produced that no socket carried. Stated, never silently truncated (R-52). */
  framesDropped: number;
  /** The last `error` event's code, for the UI to key its own copy off. `null` if none. */
  errorCode: WsErrorCode | null;
  /** Client-owned static text. Never server text, never interpolated (rules.md R-17). */
  detail: string | null;
}

export interface StreamConfig {
  /** From `api.streamUrl()`. Scheme is derived from the API base, never configured twice. */
  url: string;
  sessionOpen: SessionOpen;
  /**
   * Mint a FRESH ticket. Called once per handshake, including every reconnect, because a ticket is
   * single-use with a `TICKET_TTL_SECONDS` lifetime (decision D-6). Reusing one produces
   * `AUTH_TICKET_INVALID`, which reads as a credential problem rather than as the replay it is.
   */
  mintTicket: () => Promise<StreamTicketResponse>;
  onEvent: (event: ServerEvent) => void;
  onStatus: (status: StreamStatus) => void;
  /** Injected for testability. Defaults to `Math.random`. */
  random?: () => number;
}

export interface StreamController {
  /** Hand over one complete frame. Dropped and counted if no socket is ready — never buffered. */
  send(samples: Int16Array): void;
  stop(): void;
  status(): StreamStatus;
}

export function openStream(config: StreamConfig): StreamController {
  const stream = new Stream(config);
  stream.begin();
  return stream;
}

class Stream implements StreamController {
  private socket: WebSocket | null = null;
  private phase: StreamPhase = 'connecting';
  private attempt = 0;
  private framesSent = 0;
  private framesDropped = 0;
  private errorCode: WsErrorCode | null = null;
  private detail: string | null = null;
  private retryTimer: number | null = null;
  private handshakeTimer: number | null = null;
  private stopped = false;
  private accepted = false;
  private readonly random: () => number;

  /**
   * Reset to `0n` for every connection. technical-design.md §6: "a resumed session is a new stream,
   * not a spliced one." Carrying the counter across a reconnect would present frames captured after a
   * gap of unknown length as contiguous, so the Gateway would build one window out of two moments.
   */
  private seq = 0n;

  constructor(private readonly config: StreamConfig) {
    this.random = config.random ?? Math.random;
  }

  begin(): void {
    void this.connect();
  }

  send(samples: Int16Array): void {
    const socket = this.socket;
    if (this.stopped || socket?.readyState !== WebSocket.OPEN || !this.accepted) {
      // Reconnecting, or the handshake has not finished. The frame is gone; capture keeps running so
      // the caller is not asked to repeat themselves for a transport problem.
      this.framesDropped += 1;
      return;
    }

    if (socket.bufferedAmount >= SEND_BUFFER_HARD_BYTES) {
      this.framesDropped += 1;
      this.detail = STATUS_TEXT.clientBackpressure;
      // Not `stop()`: the close handler runs the retry path, and 4000 is not in
      // RETRYABLE_CLOSE_CODES, so retryability is decided explicitly here.
      this.abandonForRetry(CLOSE_CLIENT_BACKPRESSURE);
      return;
    }
    if (socket.bufferedAmount >= SEND_BUFFER_SOFT_BYTES) {
      this.framesDropped += 1;
      this.emit();
      return;
    }

    let frame: ArrayBuffer;
    try {
      frame = encodeFrame(this.seq, samples);
    } catch {
      // A malformed frame is a client bug. Dropping it keeps the sequence contiguous, which is what
      // lets the stream survive; sending it would close the connection with `PROTO_FRAME_SIZE`.
      this.framesDropped += 1;
      return;
    }

    socket.send(frame);
    // Incremented only for a frame that actually went out. A dropped frame must NOT consume a
    // sequence number: to the Gateway a gap and a duplicate are the same `PROTO_SEQUENCE` error, so
    // "helpfully" skipping a number to record the loss closes the stream.
    this.seq += 1n;
    this.framesSent += 1;
    if (this.framesSent % FRAMES_PER_HOP === 0) this.emit();
  }

  stop(): void {
    if (this.stopped) return;
    this.stopped = true;
    this.clearTimers();
    this.teardown(CLOSE_NORMAL);
    this.phase = 'closed';
    this.detail = STATUS_TEXT.stoppedByOperator;
    this.emit();
  }

  status(): StreamStatus {
    return {
      phase: this.phase,
      attempt: this.attempt,
      framesSent: this.framesSent,
      framesDropped: this.framesDropped,
      errorCode: this.errorCode,
      detail: this.detail,
    };
  }

  // --- connection lifecycle ----------------------------------------------------------------------

  private async connect(): Promise<void> {
    if (this.stopped) return;
    this.accepted = false;
    this.seq = 0n;
    this.emit();

    let ticket: StreamTicketResponse;
    try {
      ticket = await this.config.mintTicket();
    } catch {
      // The thrown error is discarded rather than surfaced: `api.ts` owns its own static text, and
      // this layer must not compose a message out of another layer's.
      this.fail(STATUS_TEXT.ticketUnavailable);
      return;
    }
    /* eslint-disable-next-line @typescript-eslint/no-unnecessary-condition --
       TypeScript's narrowing from the check at the top of this method does not reset across the `await`
       above, so it believes this is always false. It is not: `stop()` can run while the ticket request
       is in flight, and without this re-check an operator who ended the session gets a socket opened
       behind them — and a single-use ticket spent on it. */
    if (this.stopped) return;

    /**
     * The ticket travels as a SUBPROTOCOL, not a query parameter (technical-design.md §2.1). A URL
     * carrying a credential lands in browser history, the CloudFront access log, and every proxy log
     * in between; `Sec-WebSocket-Protocol` lands in none of them.
     *
     * The Gateway pre-assembles the value, so it is offered verbatim — and checked, because a
     * malformed entry presents as `AUTH_TICKET_MISSING` rather than as the mistake it is.
     */
    const offered = ticket.subprotocol.startsWith(WS_TICKET_SUBPROTOCOL_PREFIX)
      ? ticket.subprotocol
      : `${WS_TICKET_SUBPROTOCOL_PREFIX}${ticket.ticket}`;

    let socket: WebSocket;
    try {
      socket = new WebSocket(this.config.url, [WS_SUBPROTOCOL, offered]);
    } catch {
      this.fail(STATUS_TEXT.transportLost);
      return;
    }
    this.socket = socket;

    this.handshakeTimer = window.setTimeout(() => {
      this.handshakeTimer = null;
      this.detail = STATUS_TEXT.handshakeTimeout;
      this.abandonForRetry(CLOSE_NORMAL);
    }, HANDSHAKE_TIMEOUT_MS);

    socket.onopen = (): void => {
      this.clearHandshakeTimer();
      if (this.stopped) {
        this.teardown(CLOSE_NORMAL);
        return;
      }

      /**
       * The server MUST select `sih-v1`. An empty `protocol` means it ignored the offer, which means
       * this is not the Gateway, or not a build that speaks this contract — either way the frames
       * about to be sent would be interpreted by something unknown. Not retried.
       */
      if (socket.protocol !== WS_SUBPROTOCOL) {
        this.detail = STATUS_TEXT.subprotocolRefused;
        this.teardown(CLOSE_NORMAL);
        this.fail(STATUS_TEXT.subprotocolRefused);
        return;
      }
      this.sendSessionOpen(socket);
    };

    socket.onmessage = (event: MessageEvent<unknown>): void => {
      // The server never sends audio, so a binary frame from it is not something to interpret.
      if (typeof event.data !== 'string') return;
      this.receive(event.data);
    };

    socket.onerror = (): void => {
      // No usable detail in a browser `error` event by design (it hides cross-origin failure reasons).
      // `onclose` always follows and carries the code, so the retry decision is made there.
    };

    socket.onclose = (event: CloseEvent): void => {
      this.clearHandshakeTimer();
      this.socket = null;
      this.accepted = false;
      if (this.stopped) return;

      // The Gateway sends `{"type":"error","code":…}` and then closes, so an app code is more specific
      // than the close code when both exist. `close_reason` is never read (rules.md R-17).
      const retryable = this.errorCode !== null
        ? RETRYABLE_CLOSE_CODES.includes(closeCodeFor(this.errorCode))
        : RETRYABLE_CLOSE_CODES.includes(event.code);

      if (retryable) {
        this.scheduleRetry();
      } else {
        this.fail(this.detail ?? this.errorText() ?? STATUS_TEXT.transportLost);
      }
    };
  }

  private sendSessionOpen(socket: WebSocket): void {
    const json = JSON.stringify(this.config.sessionOpen);
    // Checked before sending, not after being closed for it. The Gateway's limit is bytes, and
    // `String.length` counts UTF-16 units, so the payload is measured the same way the server will.
    if (new TextEncoder().encode(json).length > MAX_TEXT_FRAME_BYTES) {
      this.teardown(CLOSE_NORMAL);
      this.fail(WS_ERROR_TEXT.PROTO_PAYLOAD_TOO_LARGE);
      return;
    }
    socket.send(json);
    // `accepted` stays false until `session.accepted` arrives. Frames sent before the server has
    // validated `session.open` would be scored against a session it has not agreed to, and a frame
    // that overtakes the text frame is `PROTO_FIRST_MESSAGE`.
  }

  private receive(raw: string): void {
    // Cheap upper bound before parsing. UTF-16 length under-counts multi-byte characters, so this is
    // an over-approximation of the byte limit rather than an exact one — enough to refuse a payload
    // large enough to be an attack on `JSON.parse`.
    if (raw.length > MAX_TEXT_FRAME_BYTES) return;

    const event = parseServerEvent(raw);
    // Unparseable, or a field out of contract. Dropped, not guessed at: a `risk.event` whose
    // `spoof_risk` failed validation must not reach the timeline as a number a person could read.
    if (event === null) return;

    if (event.type === 'session.accepted') {
      this.accepted = true;
      this.phase = 'streaming';
      this.attempt = 0;
      this.errorCode = null;
      this.detail = null;
      this.emit();
    } else if (event.type === 'error') {
      this.errorCode = event.code;
      this.detail = WS_ERROR_TEXT[event.code];
      this.emit();
    }

    this.config.onEvent(event);
  }

  private scheduleRetry(): void {
    if (this.attempt >= MAX_RECONNECT_ATTEMPTS) {
      this.fail(STATUS_TEXT.retriesExhausted);
      return;
    }
    const delay = backoffDelayMs(this.attempt, this.random);
    this.attempt += 1;
    this.phase = 'reconnecting';
    this.detail ??= STATUS_TEXT.transportLost;
    this.emit();

    this.retryTimer = window.setTimeout(() => {
      this.retryTimer = null;
      void this.connect();
    }, delay);
  }

  /** Close the current socket and take the retry path regardless of what code the server would send. */
  private abandonForRetry(code: number): void {
    this.teardown(code);
    this.socket = null;
    this.accepted = false;
    if (!this.stopped) this.scheduleRetry();
  }

  private fail(detail: string): void {
    this.phase = 'closed';
    this.detail = detail;
    this.clearTimers();
    this.emit();
  }

  private errorText(): string | null {
    return this.errorCode === null ? null : WS_ERROR_TEXT[this.errorCode];
  }

  private teardown(code: number): void {
    const socket = this.socket;
    if (socket === null) return;
    socket.onopen = null;
    socket.onmessage = null;
    socket.onerror = null;
    socket.onclose = null;
    // No close reason string. A reason is attacker-visible and operator-visible text on a channel
    // that must never carry anything derived from input (rules.md R-17).
    try {
      socket.close(code);
    } catch {
      // Already closing or closed.
    }
    this.socket = null;
  }

  private clearHandshakeTimer(): void {
    if (this.handshakeTimer !== null) {
      window.clearTimeout(this.handshakeTimer);
      this.handshakeTimer = null;
    }
  }

  private clearTimers(): void {
    this.clearHandshakeTimer();
    if (this.retryTimer !== null) {
      window.clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
  }

  private emit(): void {
    this.config.onStatus(this.status());
  }
}

/**
 * App code → WS close code, per the table in technical-design.md §2.5.
 *
 * Used only to decide retryability from an `error` event that arrived before the close frame, which
 * is the common ordering. `Record<WsErrorCode, number>` keeps it exhaustive.
 */
const CLOSE_CODE_FOR: Record<WsErrorCode, number> = {
  AUTH_TICKET_MISSING: 1008,
  AUTH_TICKET_INVALID: 1008,
  AUTH_ORIGIN_DENIED: 1008,
  PROTO_FRAME_SIZE: 1003,
  PROTO_SEQUENCE: 1003,
  PROTO_FIRST_MESSAGE: 1003,
  PROTO_PURPOSE_MISMATCH: 1008,
  PROTO_PAYLOAD_TOO_LARGE: 1009,
  SESSION_ALREADY_STREAMING: 1008,
  BACKPRESSURE_REJECT: 1013,
  SCORER_UNAVAILABLE: 1011,
};

function closeCodeFor(code: WsErrorCode): number {
  return CLOSE_CODE_FOR[code];
}
