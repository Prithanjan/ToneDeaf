/**
 * Bind the session before any audio exists: call reference, purpose, context value band.
 *
 * `purpose_code` and `context_value_band` are CLOSED enums from `contracts/openapi.yaml`, so both are
 * `<select>` elements built from the frozen arrays in `lib/types.ts` — never free text. A free-text
 * purpose would fail at the WSS handshake with `PROTO_PURPOSE_MISMATCH`, because the value is bound
 * server-side here and re-checked in `session.open` (decision D-4). Sourcing the options from the same
 * array the type is derived from means the list and the type cannot drift.
 *
 * The call reference is the one field in this application that ever holds a raw human-readable
 * identifier (rules.md R-16). It lives in this component's state, goes into one request body, and is
 * dropped by `App.tsx` as soon as the Gateway returns a pseudonym. It is never put in a URL, a query
 * string, `localStorage`, or a log — and `autoComplete="off"` keeps the browser from filing it in a
 * form-fill store this code does not control.
 */

import { useId, useState } from 'react';
import type { ReactElement } from 'react';
import {
  CONTEXT_VALUE_BANDS,
  MAX_CALL_REF_LENGTH,
  PURPOSE_CODES,
} from '../lib/types';
import type { ContextValueBand, PurposeCode } from '../lib/types';
import styles from './SessionSetup.module.css';

export interface SessionSetupValues {
  clientCallRef: string;
  purposeCode: PurposeCode;
  contextValueBand: ContextValueBand;
}

export interface SessionSetupProps {
  onSubmit: (values: SessionSetupValues) => void;
  /** True while the session, microphone, and stream are being brought up. */
  busy: boolean;
  /**
   * Client-owned static text from `api.ts`, `auth.ts`, or `capture.ts`. Never a server string
   * (rules.md R-17) — this value is rendered as-is and React escapes it, but escaping is not the
   * point: server prose has no place in a message this client is responsible for.
   */
  error: string | null;
}

/** Human labels for the closed set. The machine value is shown alongside, never replaced. */
const PURPOSE_LABELS: Record<PurposeCode, string> = {
  payment_release: 'Release a payment',
  beneficiary_change: 'Change a beneficiary',
  account_recovery: 'Recover an account',
  support_enquiry: 'General support enquiry',
};

/**
 * `context_value_band` is a BAND, never an amount (decision D-5). An exact figure would put the value
 * of the transaction in the audit trail, and the policy engine only ever needed the band.
 *
 * Note the collision hazard: this `high` is transaction value, and `risk_state`'s `high` is evidence.
 * The labels say which, because two unqualified "high"s on one screen is how a demo gets misread.
 */
const BAND_LABELS: Record<ContextValueBand, string> = {
  low: 'Low value',
  medium: 'Medium value',
  high: 'High value',
  unspecified: 'Not specified',
};

export function SessionSetup({ onSubmit, busy, error }: SessionSetupProps): ReactElement {
  const [callRef, setCallRef] = useState('');
  const [purposeCode, setPurposeCode] = useState<PurposeCode>('payment_release');
  const [contextValueBand, setContextValueBand] = useState<ContextValueBand>('unspecified');
  const [touched, setTouched] = useState(false);

  const callRefId = useId();
  const callRefHintId = useId();
  const purposeId = useId();
  const bandId = useId();
  const errorId = useId();

  const trimmed = callRef.trim();
  const refIsEmpty = trimmed.length === 0;
  // Shown only after interaction. The browser paints `:invalid` on first render, which would mark an
  // untouched field as wrong before anyone typed — suppressed in `global.css`, decided here.
  const showRefProblem = touched && refIsEmpty;

  return (
    <form
      className={styles.form}
      onSubmit={(event) => {
        event.preventDefault();
        setTouched(true);
        if (refIsEmpty || busy) return;
        onSubmit({ clientCallRef: trimmed, purposeCode, contextValueBand });
      }}
    >
      <h1 className={styles.heading}>Set up the session</h1>
      <p className={styles.lede}>
        These are recorded before the microphone opens, so the purpose a decision was made under cannot
        be chosen after seeing the score.
      </p>

      <div className={styles.field}>
        <label className={styles.label} htmlFor={callRefId}>
          Call reference
        </label>
        <input
          id={callRefId}
          // Not `name="reference"` or anything a browser recognises: an autofill heuristic that
          // matches would store this value in a place this code cannot clear.
          name="vi-call-ref"
          type="text"
          inputMode="text"
          autoComplete="off"
          autoCorrect="off"
          spellCheck={false}
          enterKeyHint="go"
          maxLength={MAX_CALL_REF_LENGTH}
          value={callRef}
          disabled={busy}
          aria-describedby={callRefHintId}
          aria-invalid={showRefProblem}
          onChange={(event) => {
            setCallRef(event.target.value);
          }}
          onBlur={() => {
            setTouched(true);
          }}
        />
        <p id={callRefHintId} className={styles.hint}>
          A case or ticket number. It is replaced by a one-way pseudonym before it reaches any stored
          record. Do not enter a phone number, an account number, or a person&rsquo;s name.
        </p>
        {showRefProblem ? (
          <p className={styles.problem} role="alert">
            A call reference is required so the decision can be traced to a case.
          </p>
        ) : null}
      </div>

      <div className={styles.field}>
        <label className={styles.label} htmlFor={purposeId}>
          Purpose
        </label>
        <select
          id={purposeId}
          name="vi-purpose-code"
          value={purposeCode}
          disabled={busy}
          onChange={(event) => {
            // The cast is safe because every `<option>` value comes from `PURPOSE_CODES` below, and
            // that array is what `PurposeCode` is derived from.
            setPurposeCode(event.target.value as PurposeCode);
          }}
        >
          {PURPOSE_CODES.map((code) => (
            <option key={code} value={code}>
              {PURPOSE_LABELS[code]}
            </option>
          ))}
        </select>
        <p className={styles.hint}>
          Sent as <span className="vi-code">{purposeCode}</span>. The policy table is keyed on this, so
          the same evidence can yield a different control step for a different purpose.
        </p>
      </div>

      <div className={styles.field}>
        <label className={styles.label} htmlFor={bandId}>
          Transaction value band
        </label>
        <select
          id={bandId}
          name="vi-context-value-band"
          value={contextValueBand}
          disabled={busy}
          onChange={(event) => {
            setContextValueBand(event.target.value as ContextValueBand);
          }}
        >
          {CONTEXT_VALUE_BANDS.map((band) => (
            <option key={band} value={band}>
              {BAND_LABELS[band]}
            </option>
          ))}
        </select>
        <p className={styles.hint}>
          A band, never an amount. Sent as <span className="vi-code">{contextValueBand}</span>.
        </p>
      </div>

      {error === null ? null : (
        <p id={errorId} className={styles.error} role="alert">
          {error}
        </p>
      )}

      <button
        type="submit"
        className={styles.submit}
        disabled={busy}
        aria-describedby={error === null ? undefined : errorId}
      >
        {busy ? 'Opening the microphone…' : 'Open the microphone and start'}
      </button>
      <p className={styles.footnote}>
        The next step asks your browser for microphone access. You can refuse it and nothing will have
        been sent.
      </p>
    </form>
  );
}
