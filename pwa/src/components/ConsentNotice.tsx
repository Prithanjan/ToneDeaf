/**
 * The gate. Nothing in this app may reach `getUserMedia` until this component reports acknowledgement.
 *
 * rules.md R-18 makes the ordering a control rather than UX polish, and `App.tsx` enforces it
 * structurally: `lib/capture.ts` — the only module that names `getUserMedia` — is behind a dynamic
 * `import()` that runs after `onAcknowledge`. So there is no code path, not even a mistaken one, where
 * the microphone opens while this card is on screen.
 *
 * The copy is the other half of the control. A notice that says "we process audio" and stops there is
 * not informed consent for a system that streams a person's voice to a spoof detector, so this states
 * the four things that are actually true and checkable: what leaves the device, what is not retained,
 * what the raw reference becomes, and what the output is not.
 */

import { useId } from 'react';
import type { ReactElement } from 'react';
import { WINDOW_MS } from '../lib/constants';
import styles from './ConsentNotice.module.css';

export interface ConsentNoticeProps {
  onAcknowledge: () => void;
  /** From `CreateSessionResponse.retention_days`; unknown before the first session exists. */
  retentionDays?: number;
  /** Rendered as a demo-only label when the identity source is the local issuer (rules.md R-05). */
  demoIssuer: boolean;
  /** `MOCK_SMOKE_MODE_NOT_A_DETECTOR` must be loud wherever it appears (rules.md R-46). */
  mockMode: boolean;
}

export function ConsentNotice({
  onAcknowledge,
  retentionDays,
  demoIssuer,
  mockMode,
}: ConsentNoticeProps): ReactElement {
  const headingId = useId();

  return (
    <section className={styles.card} aria-labelledby={headingId}>
      <h1 id={headingId} className={styles.heading}>
        Before the microphone opens
      </h1>

      {mockMode ? (
        <p className={styles.mock} role="status">
          <strong>MOCK_SMOKE_MODE_NOT_A_DETECTOR.</strong> This build returns fixed placeholder scores.
          Nothing on the next screen is a measurement of anything.
        </p>
      ) : null}

      <dl className={styles.terms}>
        <div className={styles.term}>
          <dt>What leaves this device</dt>
          <dd>
            Microphone audio, as {String(WINDOW_MS / 1_000)}-second overlapping windows of raw
            waveform, over an encrypted connection. No transcript is produced and no words are
            recognised.
          </dd>
        </div>

        <div className={styles.term}>
          <dt>What is not kept</dt>
          <dd>
            <strong>No raw audio is retained.</strong> It is not written to storage on this device, not
            saved in a database, and not written to any log. It exists in server memory long enough to
            be scored and is cleared when the session ends. This page stores no audio at all.
          </dd>
        </div>

        <div className={styles.term}>
          <dt>The call reference you type</dt>
          <dd>
            It is replaced by a one-way pseudonym before it reaches any stored record. Type a case or
            ticket number. Do not type a phone number, an account number, or a person&rsquo;s name.
          </dd>
        </div>

        <div className={styles.term}>
          <dt>What the result is not</dt>
          <dd>
            This estimates whether the audio carries synthetic-speech artifacts. It is not identity
            verification, not a lie detector, and not a finding about any person. The outcome is a
            control step — carry on, verify, hold, or escalate — and a human decides what to do with
            it.
          </dd>
        </div>

        {retentionDays === undefined ? null : (
          <div className={styles.term}>
            <dt>Audit retention</dt>
            <dd>
              Decision records — timestamps, scores, versions, and the pseudonym, never audio — are
              kept for <span className="vi-num">{String(retentionDays)}</span> days.
            </dd>
          </div>
        )}
      </dl>

      {demoIssuer ? (
        <p className={styles.demo}>
          <strong>Demo sign-in.</strong> This deployment mints operator tokens without a password. It
          is a local test harness, not authentication and not role-based access control.
        </p>
      ) : null}

      <button type="button" className={styles.acknowledge} onClick={onAcknowledge}>
        I have read this — set up the session
      </button>

      <p className={styles.footnote}>
        The microphone stays closed until you finish the next screen. Nothing is being recorded now.
      </p>
    </section>
  );
}
