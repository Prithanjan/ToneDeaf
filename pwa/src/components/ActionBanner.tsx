/**
 * The control step. Four values, and the fifth is a compile error.
 *
 * `Action` is a union of exactly `continue | verify | hold | escalate` (rules.md R-07), and every
 * lookup below is a `Record<Action, …>` so adding a member to the union breaks the build here rather
 * than rendering an empty banner. The words this vocabulary does not contain are the point: nothing in
 * this system asserts that a transaction is legitimate or that a caller is a fraudster. Each of the
 * four is a step a human still owns, and the copy says so explicitly, because a banner that reads as a
 * verdict is the failure mode the closed vocabulary exists to prevent.
 *
 * The banner also carries what makes the step reviewable — reason code, policy version, audit event id
 * — and states when the mechanism is a rehearsal rather than a decision (rules.md R-01 / R-11). A
 * control step derived from an artifact below `policy_eligible` is a demonstration of the wiring, and
 * saying so on the same surface as the action is the only place it cannot be missed.
 */

import type { ReactElement } from 'react';
import type { Action, ArtifactState, ReasonCode, RiskState } from '../lib/types';
import styles from './ActionBanner.module.css';

export interface ActionBannerProps {
  /** `null` until the first `policy.action`. Not defaulted to `continue` — see below. */
  action: Action | null;
  reasonCode: ReasonCode | null;
  riskState: RiskState;
  policyVersion: string | null;
  auditEventId?: string;
  /** True once `high` has been observed. Sticky for the session (rules.md R-13). */
  stickyHigh: boolean;
  artifactState?: ArtifactState;
}

const ACTION_LABEL: Record<Action, string> = {
  continue: 'Carry on',
  verify: 'Verify the caller',
  hold: 'Hold the request',
  escalate: 'Escalate to review',
};

/**
 * What each step asks a person to do. Every sentence names the human as the actor.
 *
 * `continue` in particular is worded as "no step required", not as clearance. "Carry on" plus a green
 * tick would be read as the system vouching for the caller, which it cannot do and never claims.
 */
const ACTION_SENTENCE: Record<Action, string> = {
  continue: 'No additional control step from this evidence. The call proceeds as it would have without this system.',
  verify: 'Ask an additional verification question before proceeding. This system does not perform the verification.',
  hold: 'Pause this request pending review. Nothing has been concluded about the caller.',
  escalate: 'Route this to a human reviewer with the window evidence attached.',
};

const REASON_SENTENCE: Record<ReasonCode, string> = {
  EVIDENCE_BELOW_K: 'Fewer of the recent eligible windows carried high-risk evidence than the evidence bar requires.',
  EVIDENCE_K_OF_N_MET: 'Enough of the recent eligible windows carried high-risk evidence to meet the evidence bar.',
  INSUFFICIENT_ELIGIBLE_WINDOWS: 'Too few windows were measurable to reach the evidence bar either way.',
  QUALITY_DEGRADED: 'Audio quality was too poor for the measurement to be relied on.',
};

const ARTIFACT_CAVEAT: Record<ArtifactState, string | null> = {
  research_only:
    'The model artifact in this build is marked research_only. This control step demonstrates the mechanism; it is not a policy decision.',
  demo_eligible:
    'The model artifact in this build is marked demo_eligible. This control step demonstrates the mechanism; it is not a policy decision.',
  policy_eligible: null,
};

export function ActionBanner({
  action,
  reasonCode,
  riskState,
  policyVersion,
  auditEventId,
  stickyHigh,
  artifactState,
}: ActionBannerProps): ReactElement {
  const caveat = artifactState === undefined ? null : ARTIFACT_CAVEAT[artifactState];

  return (
    <section
      className={styles.banner}
      // Absent rather than defaulted. `data-vi-action="continue"` before any decision would show a
      // step the policy engine never issued, and "carry on" is the one it would be most harmful to
      // invent.
      data-vi-action={action ?? undefined}
      data-vi-state={action === null ? riskState : undefined}
      aria-live="polite"
      aria-atomic="true"
      tabIndex={-1}
    >
      <div className={styles.row}>
        {action === null ? null : <ActionGlyph action={action} />}
        <div className={styles.text}>
          <p className={styles.label}>{action === null ? 'No control step yet' : ACTION_LABEL[action]}</p>
          <p className={styles.sentence}>
            {action === null
              ? 'The policy engine has not issued a step for this session yet. Nothing has been decided.'
              : ACTION_SENTENCE[action]}
          </p>
        </div>
      </div>

      {reasonCode === null ? null : (
        <p className={styles.reason}>
          <span className="vi-code">{reasonCode}</span>
          <span>{REASON_SENTENCE[reasonCode]}</span>
        </p>
      )}

      {stickyHigh ? (
        <p className={styles.sticky}>
          <strong>High stays high for this session.</strong> Evidence does not expire because a later
          window looked clean. Clearing it needs an explicit human resolution step, which is backlog and
          not built in this release.
        </p>
      ) : null}

      {caveat === null ? null : <p className={styles.caveat}>{caveat}</p>}

      <dl className={styles.provenance}>
        <div className={styles.provenanceItem}>
          <dt>Policy</dt>
          <dd className="vi-code">{policyVersion ?? '—'}</dd>
        </div>
        <div className={styles.provenanceItem}>
          <dt>Audit event</dt>
          {/* The id, not the record: it is the handle a reviewer uses to pull the hash-chained row. */}
          <dd className="vi-code">{auditEventId ?? '—'}</dd>
        </div>
      </dl>
    </section>
  );
}

/**
 * Action glyphs. Distinct silhouettes with no verdict semantics.
 *
 * Deliberately NOT a tick or a cross: a tick on `continue` and a cross on `hold` would smuggle a
 * verdict vocabulary in through the icon set, which is the substitution rules.md R-07 exists to make
 * impossible. An arrow, a question, a pause, and an arrow-to-a-ceiling describe movements, not
 * judgements.
 */
function ActionGlyph({ action }: { action: Action }): ReactElement {
  return (
    <svg className={styles.glyph} viewBox="0 0 24 24" role="presentation" focusable="false">
      {action === 'continue' ? (
        <>
          <line x1="4" y1="12" x2="18" y2="12" />
          <polyline points="13,7 18,12 13,17" />
        </>
      ) : null}
      {action === 'verify' ? (
        <>
          <circle cx="12" cy="12" r="8.5" />
          <path d="M9.4 9.4a2.7 2.7 0 1 1 2.7 3.2v1.2" />
          <line x1="12.1" y1="16.6" x2="12.1" y2="16.7" />
        </>
      ) : null}
      {action === 'hold' ? (
        <>
          <line x1="9" y1="5" x2="9" y2="19" />
          <line x1="15" y1="5" x2="15" y2="19" />
        </>
      ) : null}
      {action === 'escalate' ? (
        <>
          <line x1="4" y1="4" x2="20" y2="4" />
          <line x1="12" y1="20" x2="12" y2="8" />
          <polyline points="7,13 12,8 17,13" />
        </>
      ) : null}
    </svg>
  );
}
