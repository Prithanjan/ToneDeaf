/**
 * Session orchestration: consent → setup → live → ended.
 *
 * The ordering in this file is the privacy control (rules.md R-18, technical-design.md §6). Two things
 * make it structural rather than a convention someone could reorder:
 *
 * 1. `lib/capture.ts` — the only module in the client that names `getUserMedia` — is reached through a
 *    dynamic `import()` inside `startSession`, which is unreachable until `ConsentNotice` has called
 *    `onAcknowledge`. Before that, the module is not even in memory.
 * 2. The raw call reference is never stored here. `startSession` receives it as an argument, passes it
 *    to `api.createSession`, and lets it fall out of scope. It lives in `SessionSetup`'s own state
 *    until that component unmounts, and nothing writes it to a ref, a URL, or web storage
 *    (rules.md R-16). Only the returned HMAC pseudonym is held in `session`.
 *
 * This component also owns the sticky `high` invariant (rules.md R-13). That is not a second policy
 * engine: the Gateway decides, and this only refuses to walk a decision backwards if a later event
 * carries a lower state.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { ReactElement } from 'react';
import { ActionBanner } from './components/ActionBanner';
import { ConsentNotice } from './components/ConsentNotice';
import { RiskTimeline } from './components/RiskTimeline';
import { SessionSetup } from './components/SessionSetup';
import type { SessionSetupValues } from './components/SessionSetup';
import { createSession, createStreamTicket, fetchVersion, streamUrl } from './lib/api';
import { getAccessToken, isDemoIssuer } from './lib/auth';
import type { CaptureSession } from './lib/capture';
import { openStream } from './lib/stream';
import type { StreamController, StreamStatus } from './lib/stream';
import type {
  Action,
  ArtifactState,
  CreateSessionResponse,
  DetectorMode,
  ReasonCode,
  RiskEvent,
  RiskState,
  ServerEvent,
  VersionInfo,
} from './lib/types';
import styles from './App.module.css';

type Phase = 'consent' | 'setup' | 'live' | 'ended';

/**
 * Error classes whose `message` this client wrote itself: `ApiError`, `AuthError`, `CaptureError`,
 * `FrameError`. Anything else — a `TypeError`, a `DOMException`, a fetch that failed at the transport
 * — carries host-defined or server-defined text and must not reach the DOM (rules.md R-17).
 *
 * Matched on `name` rather than `instanceof` for one specific reason: importing `CaptureError` as a
 * VALUE would load `lib/capture.ts` at startup, which puts the `getUserMedia` call site in memory
 * before consent and defeats the gate above. Do not widen this to `error instanceof Error`.
 */
const CLIENT_OWNED_ERRORS = new Set(['ApiError', 'AuthError', 'CaptureError', 'FrameError']);

const GENERIC_FAILURE = 'The session could not be started. Nothing was sent.';

function describeFailure(error: unknown): string {
  if (error instanceof Error && CLIENT_OWNED_ERRORS.has(error.name) && error.message.length > 0) {
    return error.message;
  }
  return GENERIC_FAILURE;
}

/** rules.md R-13: `high` never walks back. Un-sticking is a human resolution step, and it is backlog. */
function advanceRiskState(current: RiskState, incoming: RiskState): RiskState {
  return current === 'high' ? 'high' : incoming;
}

export function App(): ReactElement {
  const [phase, setPhase] = useState<Phase>('consent');
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const [session, setSession] = useState<CreateSessionResponse | null>(null);
  const [version, setVersion] = useState<VersionInfo | null>(null);

  const [windows, setWindows] = useState<RiskEvent[]>([]);
  const [riskState, setRiskState] = useState<RiskState>('collecting');
  const [action, setAction] = useState<Action | null>(null);
  const [reasonCode, setReasonCode] = useState<ReasonCode | null>(null);
  const [auditEventId, setAuditEventId] = useState<string | undefined>(undefined);
  const [evidenceWindowCount, setEvidenceWindowCount] = useState<number | undefined>(undefined);
  const [evidenceHighCount, setEvidenceHighCount] = useState<number | undefined>(undefined);
  const [streamStatus, setStreamStatus] = useState<StreamStatus | null>(null);
  const [detectorMode, setDetectorMode] = useState<DetectorMode | undefined>(undefined);
  const [artifactState, setArtifactState] = useState<ArtifactState | undefined>(undefined);
  const [bufferCleared, setBufferCleared] = useState(false);

  const captureRef = useRef<CaptureSession | null>(null);
  const streamRef = useRef<StreamController | null>(null);

  const demoIssuer = isDemoIssuer();
  const mockMode = (detectorMode ?? version?.detector_mode) === 'MOCK_SMOKE_MODE_NOT_A_DETECTOR';
  const effectiveArtifactState = artifactState ?? session?.artifact_state ?? version?.artifact_state;

  /**
   * The parity set is read before anything else so `detector_mode` and `artifact_state` are on screen
   * from the first paint. Mock mode has to be loud everywhere it appears (rules.md R-46), and a label
   * that only shows up after a stream opens has already missed the screenshot.
   */
  useEffect(() => {
    let live = true;
    void fetchVersion()
      .then((info) => {
        if (live) setVersion(info);
      })
      .catch(() => {
        // A failed metadata read is not a session failure and does not get an error banner. The
        // absence of the versions is itself the honest signal, and the header renders it as unknown.
      });
    return () => {
      live = false;
    };
  }, []);

  const teardown = useCallback(() => {
    captureRef.current?.stop();
    captureRef.current = null;
    streamRef.current?.stop();
    streamRef.current = null;
  }, []);

  // Stop the microphone if this component ever unmounts. A live `MediaStreamTrack` outliving the
  // React tree is a recording indicator with nothing behind it.
  useEffect(() => teardown, [teardown]);

  const handleEvent = useCallback((event: ServerEvent) => {
    switch (event.type) {
      case 'session.accepted':
        setDetectorMode(event.detector_mode);
        setArtifactState(event.artifact_state);
        break;

      case 'risk.event':
        setWindows((previous) => {
          // Deduplicated by `window_seq`. A reconnect restarts the frame sequence, so the Gateway may
          // re-score a window number this client already holds; keeping both would draw one moment
          // twice and inflate the visible evidence.
          const index = previous.findIndex((held) => held.window_seq === event.window_seq);
          if (index === -1) return [...previous, event];
          const merged = [...previous];
          merged[index] = event;
          return merged;
        });
        setRiskState((current) => advanceRiskState(current, event.risk_state));
        break;

      case 'policy.action':
        setAction(event.action);
        setReasonCode(event.reason_code);
        setAuditEventId(event.audit_event_id);
        setEvidenceWindowCount(event.evidence_window_count);
        setEvidenceHighCount(event.evidence_high_count);
        setRiskState((current) => advanceRiskState(current, event.risk_state));
        break;

      case 'session.closed':
        // `buffer_cleared` is the Gateway asserting it zeroed the volatile PCM ring (rules.md R-14).
        // Surfaced because an unverifiable privacy claim is worth less than a stated one.
        setBufferCleared(event.buffer_cleared);
        break;

      case 'error':
        // Text comes from `stream.ts`'s own table via `StreamStatus.errorCode`; nothing to do here.
        break;
    }
  }, []);

  const handleStatus = useCallback((status: StreamStatus) => {
    setStreamStatus(status);
    if (status.phase === 'closed') {
      // The socket is gone for good. Stop the microphone in the same turn: capture that outlives the
      // stream is audio being taken with nowhere to send it.
      captureRef.current?.stop();
      captureRef.current = null;
      setPhase('ended');
    }
  }, []);

  const startSession = useCallback(
    async (values: SessionSetupValues): Promise<void> => {
      setBusy(true);
      setFailure(null);

      let capture: CaptureSession | null = null;
      try {
        const accessToken = await getAccessToken();

        /**
         * Consent has been acknowledged, so the capture module may now be loaded. Order within this
         * try matters twice over:
         *
         * - The microphone is acquired BEFORE the session is created, so a refused permission prompt
         *   leaves no server-side session record and no retention clock running for a call that never
         *   happened.
         * - The stream ticket is minted by `openStream` AFTER the prompt has been answered. A ticket
         *   minted first would spend its `TICKET_TTL_SECONDS` waiting on a browser dialog and fail the
         *   handshake with `AUTH_TICKET_INVALID` (decision D-6).
         */
        const { acquireMicrophone } = await import('./lib/capture');
        capture = await acquireMicrophone();

        // `values.clientCallRef` is used here and never stored. What comes back is the pseudonym.
        const created = await createSession(accessToken, values);
        setSession(created);

        const controller = openStream({
          url: streamUrl(),
          sessionOpen: {
            type: 'session.open',
            call_ref: created.call_ref,
            purpose_code: created.purpose_code,
            context_value_band: created.context_value_band,
            client_capture: capture.descriptor,
          },
          mintTicket: () => createStreamTicket(accessToken, created.session_id),
          onEvent: handleEvent,
          onStatus: handleStatus,
        });

        captureRef.current = capture;
        streamRef.current = controller;

        // Frames start flowing now. `stream.send` drops anything that arrives before
        // `session.accepted`, so nothing is queued while the handshake completes (rules.md R-20).
        await capture.start((samples) => {
          controller.send(samples);
        });

        setPhase('live');
      } catch (error) {
        capture?.stop();
        captureRef.current = null;
        streamRef.current?.stop();
        streamRef.current = null;
        setFailure(describeFailure(error));
      } finally {
        setBusy(false);
      }
    },
    [handleEvent, handleStatus],
  );

  return (
    <div className={styles.shell}>
      <a className="vi-skip-link" href="#vi-decision">
        Skip to the current control step
      </a>

      <header className={styles.header}>
        <p className={styles.wordmark}>Voice Integrity Control Plane</p>
        <ul className={styles.badges} role="list">
          {mockMode ? (
            <li className={styles.badgeLoud}>MOCK_SMOKE_MODE_NOT_A_DETECTOR</li>
          ) : null}
          {demoIssuer ? <li className={styles.badge}>Demo sign-in — not authentication</li> : null}
          {effectiveArtifactState === undefined ? null : (
            <li className={effectiveArtifactState === 'policy_eligible' ? styles.badge : styles.badgeLoud}>
              artifact: <span className="vi-code">{effectiveArtifactState}</span>
            </li>
          )}
          {phase === 'live' || phase === 'ended' ? (
            /* rules.md R-01: the deprecated capture path is declared at runtime, not only in a doc. */
            <li className={styles.badge}>
              capture: <span className="vi-code">scriptprocessor</span>
            </li>
          ) : null}
        </ul>
      </header>

      <main className={styles.main}>
        {phase === 'consent' ? (
          <ConsentNotice
            onAcknowledge={() => {
              setPhase('setup');
            }}
            retentionDays={session?.retention_days}
            demoIssuer={demoIssuer}
            mockMode={mockMode}
          />
        ) : null}

        {phase === 'setup' ? (
          <SessionSetup
            onSubmit={(values) => {
              void startSession(values);
            }}
            busy={busy}
            error={failure}
          />
        ) : null}

        {phase === 'live' || phase === 'ended' ? (
          <>
            <div id="vi-decision">
              <ActionBanner
                action={action}
                reasonCode={reasonCode}
                riskState={riskState}
                policyVersion={session?.policy_version ?? null}
                auditEventId={auditEventId}
                stickyHigh={riskState === 'high'}
                artifactState={effectiveArtifactState}
              />
            </div>

            <RiskTimeline
              windows={windows}
              riskState={riskState}
              probabilityLanguagePermitted={session?.probability_language_permitted === true}
              evidenceWindowCount={evidenceWindowCount}
              evidenceHighCount={evidenceHighCount}
            />

            <section className={styles.transport} aria-label="Stream health">
              <dl className={styles.transportGrid}>
                <div>
                  <dt>Stream</dt>
                  <dd>{streamStatus?.phase ?? 'connecting'}</dd>
                </div>
                <div>
                  <dt>Frames sent</dt>
                  <dd className="vi-num">{String(streamStatus?.framesSent ?? 0)}</dd>
                </div>
                <div>
                  {/* Stated, not hidden. A bounded loss reported as zero reads as "nothing was lost"
                      (rules.md R-52). */}
                  <dt>Frames dropped</dt>
                  <dd className="vi-num">{String(streamStatus?.framesDropped ?? 0)}</dd>
                </div>
                <div>
                  <dt>Reconnects</dt>
                  <dd className="vi-num">{String(streamStatus?.attempt ?? 0)}</dd>
                </div>
              </dl>

              {streamStatus?.detail === null || streamStatus?.detail === undefined ? null : (
                <p className={styles.transportDetail} role="status">
                  {streamStatus.detail}
                </p>
              )}

              {phase === 'live' ? (
                <button
                  type="button"
                  className={styles.stop}
                  onClick={() => {
                    teardown();
                    setPhase('ended');
                  }}
                >
                  Stop the microphone and end the session
                </button>
              ) : (
                <p className={styles.ended}>
                  The microphone is closed.{' '}
                  {bufferCleared
                    ? 'The Gateway reported its audio buffer was cleared.'
                    : 'No audio was written to storage at any point.'}
                </p>
              )}
            </section>
          </>
        ) : null}
      </main>

      <footer className={styles.footer}>
        <dl className={styles.provenance}>
          <div>
            <dt>commit</dt>
            <dd className="vi-code">{version?.git_commit ?? 'unknown'}</dd>
          </div>
          <div>
            <dt>profile</dt>
            <dd className="vi-code">{version?.deployment_profile ?? 'unknown'}</dd>
          </div>
          <div>
            <dt>model</dt>
            <dd className="vi-code">{version?.model_version ?? 'unknown'}</dd>
          </div>
          <div>
            <dt>calibration</dt>
            <dd className="vi-code">{version?.calibration_version ?? 'unknown'}</dd>
          </div>
          <div>
            <dt>provider</dt>
            <dd className="vi-code">{version?.execution_provider ?? 'unknown'}</dd>
          </div>
        </dl>
        <p className={styles.disclaimer}>
          Prevention-control demonstration. This system estimates synthetic-speech artifacts in audio;
          it does not measure fraud and no fraud-reduction claim is made from it.
        </p>
      </footer>
    </div>
  );
}
