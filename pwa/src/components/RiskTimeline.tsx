/**
 * Per-window evidence. Never a bare score.
 *
 * Four honesty properties, each of which is a specific thing this component refuses to draw:
 *
 * 1. **Ineligible windows are not short bars.** A voicing-ineligible window is absence of evidence
 *    (rules.md R-09), so it renders as a hatched stub of FIXED height at the axis. Drawing it at a
 *    height derived from its score would say "measured, and low" about a window the policy engine
 *    skipped — inventing a measurement out of a refusal to measure.
 *
 * 2. **No client-side thresholding.** A cell's colour comes from `risk.event.risk_state`, which the
 *    Gateway computed; its height comes from `spoof_risk`, which the Scorer produced. This component
 *    has no threshold of its own. A local "is this high?" boundary would be a second, uncalibrated
 *    policy engine whose disagreements with `policy/thresholds.yaml` nobody could explain (R-12).
 *
 * 3. **`collecting` gets no progress bar.** The first decision needs `WINDOW_MS` of VOICED audio,
 *    which is more wall clock than `WINDOW_MS` — how much more depends on how much the caller talks.
 *    A determinate bar is a countdown promise the system cannot keep, so `collecting` shows an
 *    indeterminate placeholder plus the honest count of eligible windows so far.
 *
 * 4. **`uncertain` is a finding, not a spinner.** It means n eligible windows were examined and fewer
 *    than k were high. That is a reportable result and it is styled as one.
 *
 * The cells are ordinal by `window_seq`, never scaled to elapsed time: hops are 640 ms of AUDIO, and
 * gaps in wall clock (silence, packet loss, a reconnect) would stretch a time axis in a way that
 * implies the detector was looking at something during the gap.
 */

import type { ReactElement } from 'react';
import { WINDOW_MS } from '../lib/constants';
import type { QualityFlag, RiskEvent, RiskState } from '../lib/types';
import styles from './RiskTimeline.module.css';

export interface RiskTimelineProps {
  /** Validated `risk.event`s in arrival order. Deduplicated by `window_seq` upstream. */
  windows: RiskEvent[];
  /** The session state, held sticky by `App.tsx` (rules.md R-13). */
  riskState: RiskState;
  /**
   * From `CreateSessionResponse.probability_language_permitted`. While `policy/calibration.json`
   * carries `status: placeholder-not-policy-eligible`, no probability language may appear in the UI
   * (rules.md R-11) — so this flag changes what the number is CALLED, never whether it is shown.
   */
  probabilityLanguagePermitted: boolean;
  /** Eligible windows the policy engine counted (n), from the latest `policy.action`. */
  evidenceWindowCount?: number;
  /** High windows among them (k). */
  evidenceHighCount?: number;
  /** Cells rendered. Older windows are dropped from the view and the drop is stated (R-52). */
  maxCells?: number;
}

const DEFAULT_MAX_CELLS = 40;

/** Fixed stub height for an unmeasured window, as a fraction of the plot. Not derived from a score. */
const INELIGIBLE_STUB = 'var(--vi-space-2)';

/**
 * Decimal places for every score a person reads, screen or screen reader (design.md §9). Four, and the
 * same four in both places: the readout and the accessible name are the same measurement, and a reader
 * who hears `0.512` while the projector shows `0.5118` has been given two different numbers for one
 * window. Fixed-point rather than a percentage on purpose — §9 forbids dressing the score up as a
 * confidence, and `--vi-font-mono` with tabular figures keeps the column from shifting every 640 ms.
 */
const SCORE_DECIMALS = 4;

const RISK_STATE_LABEL: Record<RiskState, string> = {
  collecting: 'Collecting',
  uncertain: 'Uncertain',
  high: 'High',
};

/**
 * What each state means, in a sentence, for the screen reader and the projector.
 *
 * `uncertain` is worded as a result because it is one. Calling it "still deciding" would turn a
 * reportable finding into a loading message and quietly remove it from the demo's outcomes.
 */
const RISK_STATE_SENTENCE: Record<RiskState, string> = {
  collecting: 'Not enough eligible windows yet to reach any finding.',
  uncertain: 'Eligible windows were examined and too few carried high-risk evidence to act on.',
  high: 'Enough eligible windows carried high-risk evidence to raise the control step.',
};

const QUALITY_FLAG_LABEL: Record<QualityFlag, string> = {
  LOW_ENERGY: 'too quiet',
  CLIPPING_DETECTED: 'clipping',
  NARROWBAND_SUSPECTED: 'narrowband channel',
  HIGH_NOISE: 'noisy',
  PACKET_LOSS_SUSPECTED: 'audio gaps',
  DC_OFFSET: 'DC offset',
  INSUFFICIENT_VOICED: 'not enough speech',
};

export function RiskTimeline({
  windows,
  riskState,
  probabilityLanguagePermitted,
  evidenceWindowCount,
  evidenceHighCount,
  maxCells = DEFAULT_MAX_CELLS,
}: RiskTimelineProps): ReactElement {
  const omitted = Math.max(0, windows.length - maxCells);
  const shown = omitted > 0 ? windows.slice(omitted) : windows;
  const latest = windows.length > 0 ? windows[windows.length - 1] : undefined;
  const eligibleCount = windows.filter((event) => event.eligible).length;

  return (
    <section className={styles.panel} aria-labelledby="vi-timeline-heading">
      <header className={styles.head}>
        <h2 id="vi-timeline-heading" className={styles.heading}>
          Window evidence
        </h2>
        <p className={styles.stateLine} data-vi-state={riskState}>
          <StateGlyph state={riskState} />
          <span className={styles.stateLabel}>{RISK_STATE_LABEL[riskState]}</span>
          <span className="vi-sr-only">{RISK_STATE_SENTENCE[riskState]}</span>
        </p>
      </header>

      <p className={styles.sentence}>{RISK_STATE_SENTENCE[riskState]}</p>

      {windows.length === 0 ? (
        /**
         * Indeterminate on purpose. `data-vi-shimmer` is the design system's single looping
         * animation; under `prefers-reduced-motion` it becomes the static hatch, which still reads as
         * "not yet a measurement" rather than as an empty slot that failed to load.
         */
        <div className={styles.awaiting} data-vi-shimmer="true" role="status">
          <span>
            Collecting voiced audio. The first window needs{' '}
            <span className="vi-num">{(WINDOW_MS / 1_000).toFixed(2)}</span> seconds of speech, so it
            arrives later than that in wall-clock time — silence does not count toward it.
          </span>
        </div>
      ) : (
        <>
          {omitted > 0 ? (
            <p className={styles.truncation}>
              Showing the most recent <span className="vi-num">{String(shown.length)}</span> windows.{' '}
              <span className="vi-num">{String(omitted)}</span> earlier{' '}
              {omitted === 1 ? 'window is' : 'windows are'} not drawn.
            </p>
          ) : null}

          <ol className={styles.plot} role="list" aria-label="Scored windows, oldest first">
            {shown.map((event) => (
              <WindowCell key={event.window_seq} event={event} />
            ))}
          </ol>

          <dl className={styles.legend}>
            <div className={styles.legendItem}>
              <span className={styles.swatch} data-vi-state="high" aria-hidden="true" />
              <dt>Counted</dt>
              <dd>bar height is the score for that window</dd>
            </div>
            <div className={styles.legendItem}>
              <span
                // Joined rather than interpolated: a CSS-module lookup is `string | undefined`, and
                // `join` drops a missing class where a template literal would write the six characters
                // `undefined` into the class attribute.
                className={[styles.swatch, styles.swatchStub, 'vi-hatch'].join(' ')}
                data-vi-state="ineligible"
                aria-hidden="true"
              />
              <dt>Skipped</dt>
              <dd>quality too poor to measure — no score is shown, and it counts toward nothing</dd>
            </div>
          </dl>
        </>
      )}

      <dl className={styles.readouts}>
        <div className={styles.readout}>
          <dt>{probabilityLanguagePermitted ? 'Latest calibrated score' : 'Latest score'}</dt>
          <dd className="vi-num">
            {latest?.eligible === true ? latest.spoof_risk.toFixed(SCORE_DECIMALS) : '—'}
          </dd>
        </div>
        <div className={styles.readout}>
          <dt>Window</dt>
          <dd className="vi-num">{latest === undefined ? '—' : String(latest.window_seq)}</dd>
        </div>
        <div className={styles.readout}>
          <dt>Eligible windows</dt>
          <dd className="vi-num">
            {String(eligibleCount)} / {String(windows.length)}
          </dd>
        </div>
        <div className={styles.readout}>
          <dt>High of counted</dt>
          <dd className="vi-num">
            {evidenceHighCount === undefined || evidenceWindowCount === undefined
              ? '—'
              : `${String(evidenceHighCount)} / ${String(evidenceWindowCount)}`}
          </dd>
        </div>
      </dl>

      {/**
       * The k and n values live in `policy/thresholds.yaml` and arrive on `policy.action`; they are
       * deliberately not restated as numbers here. Writing "3 of 5" in the client would be a second
       * definition of a policy constant, and it would go stale silently the day the bar moves.
       */}
      <p className={styles.footnote}>
        {probabilityLanguagePermitted
          ? 'Scores are calibrated. A high score is evidence about the audio, not a finding about a person.'
          : 'Scores are not calibrated in this build, so the number is an ordering, not a probability.'}{' '}
        A raised control step needs several high windows among the recent eligible ones — one high
        window never raises it on its own.
      </p>

      {latest !== undefined && latest.quality_flags.length > 0 ? (
        <ul className={styles.flags} role="list" aria-label="Audio quality notes on the latest window">
          {latest.quality_flags.map((flag) => (
            <li key={flag} className={styles.flag}>
              <span className="vi-code">{flag}</span>
              <span className={styles.flagLabel}>{QUALITY_FLAG_LABEL[flag]}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

function WindowCell({ event }: { event: RiskEvent }): ReactElement {
  const flagText =
    event.quality_flags.length === 0
      ? ''
      : ` Quality notes: ${event.quality_flags.map((flag) => QUALITY_FLAG_LABEL[flag]).join(', ')}.`;

  if (!event.eligible) {
    return (
      <li className={styles.cell}>
        <span
          className={[styles.bar, styles.stub, 'vi-hatch'].join(' ')}
          data-vi-state="ineligible"
          // Height is a constant, not `spoof_risk`. There is no measurement to encode.
          style={{ height: INELIGIBLE_STUB }}
        />
        <span className="vi-sr-only">
          Window {String(event.window_seq)}: skipped, quality too poor to measure. No score.
          {flagText}
        </span>
      </li>
    );
  }

  return (
    <li className={styles.cell}>
      <span
        className={styles.bar}
        data-vi-state={event.risk_state}
        // The plot height is a token; the fraction is the server's score. No pixel arithmetic here.
        // `SCORE_DECIMALS` is reused for the ratio only because a coarser string would quantise the
        // bar heights; this one is a layout value, not a number anyone reads.
        style={{ height: `calc(var(--vi-timeline-h) * ${event.spoof_risk.toFixed(SCORE_DECIMALS)})` }}
      />
      <span className="vi-sr-only">
        Window {String(event.window_seq)}: score {event.spoof_risk.toFixed(SCORE_DECIMALS)}, counted,
        session state {RISK_STATE_LABEL[event.risk_state]}.{flagText}
      </span>
    </li>
  );
}

/**
 * State glyphs. Distinct silhouettes, not four coloured dots.
 *
 * State is never carried by colour alone: every state renders a label, a sentence, and a shape that
 * survives greyscale print and Windows forced-colors, where the whole palette collapses to one pair.
 * `stroke: currentColor` in `global.css` means the glyph cannot disagree with its chip's colour.
 */
function StateGlyph({ state }: { state: RiskState }): ReactElement {
  return (
    <svg className={styles.glyph} viewBox="0 0 24 24" role="presentation" focusable="false">
      {state === 'collecting' ? (
        /* Accumulating bars: something is being gathered, and nothing is claimed yet. */
        <>
          <line x1="5" y1="18" x2="5" y2="14" />
          <line x1="12" y1="18" x2="12" y2="10" />
          <line x1="19" y1="18" x2="19" y2="6" />
        </>
      ) : null}
      {state === 'uncertain' ? (
        /* A diamond: examined, balanced on a point, not resolved. */
        <path d="M12 3.5 20.5 12 12 20.5 3.5 12Z" />
      ) : null}
      {state === 'high' ? (
        <>
          <path d="M12 3.8 21.2 20H2.8Z" />
          <line x1="12" y1="10" x2="12" y2="14" />
          <line x1="12" y1="17" x2="12" y2="17.1" />
        </>
      ) : null}
    </svg>
  );
}
