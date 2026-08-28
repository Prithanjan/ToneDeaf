/**
 * Wire types and pure validators for the REST and WebSocket surfaces.
 *
 * Every union below mirrors a CLOSED enum in `contracts/openapi.yaml`, which Pair A owns under a
 * two-key rule (rules.md R-22). Nothing here may introduce a value the schema does not carry.
 *
 * The unions are derived from frozen arrays (`(typeof ARR)[number]`) rather than written twice. That
 * is not brevity: the same array drives the `<select>` options in `SessionSetup`, so the set the
 * user can choose from and the set the type permits cannot drift apart. A drifted `purpose_code`
 * fails at the WSS handshake with `PROTO_PURPOSE_MISMATCH`, which reads as an auth bug.
 *
 * The validators are PURE — no clock, no I/O, no module state (rules.md R-53). They exist because
 * everything arriving from the network is untrusted at the type level: a `ServerEvent` that
 * type-checks only because we cast it is a runtime hole in the one part of the UI that is supposed
 * to be an honest account of the evidence.
 */

// --- Closed enums from contracts/openapi.yaml -----------------------------------------------------

export const PURPOSE_CODES = [
  'payment_release',
  'beneficiary_change',
  'account_recovery',
  'support_enquiry',
] as const;
export type PurposeCode = (typeof PURPOSE_CODES)[number];

/** Decision D-5: a closed band, never a free string and never an amount. */
export const CONTEXT_VALUE_BANDS = ['low', 'medium', 'high', 'unspecified'] as const;
export type ContextValueBand = (typeof CONTEXT_VALUE_BANDS)[number];

export const RISK_STATES = ['collecting', 'uncertain', 'high'] as const;
export type RiskState = (typeof RISK_STATES)[number];

/**
 * The complete action vocabulary (rules.md R-07). Four members, typed as a literal union so a fifth
 * is a compile error at every `Record<Action, …>` in the UI rather than a label nobody wrote.
 *
 * The words this list does not contain are the point of the rule: no action in this system asserts
 * that a transaction is legitimate or that a caller is a fraudster. Every member is a control step a
 * human still owns.
 */
export const ACTIONS = ['continue', 'verify', 'hold', 'escalate'] as const;
export type Action = (typeof ACTIONS)[number];

export const REASON_CODES = [
  'EVIDENCE_BELOW_K',
  'EVIDENCE_K_OF_N_MET',
  'INSUFFICIENT_ELIGIBLE_WINDOWS',
  'QUALITY_DEGRADED',
] as const;
export type ReasonCode = (typeof REASON_CODES)[number];

export const QUALITY_FLAGS = [
  'LOW_ENERGY',
  'CLIPPING_DETECTED',
  'NARROWBAND_SUSPECTED',
  'HIGH_NOISE',
  'PACKET_LOSS_SUSPECTED',
  'DC_OFFSET',
  'INSUFFICIENT_VOICED',
] as const;
export type QualityFlag = (typeof QUALITY_FLAGS)[number];

export type DetectorMode = 'REAL_DETECTOR' | 'MOCK_SMOKE_MODE_NOT_A_DETECTOR';
export type DeploymentProfile = 'aws-gpu' | 'local-cpu';
export type ArtifactState = 'research_only' | 'demo_eligible' | 'policy_eligible';

/**
 * WSS application error codes (technical-design.md §2.5).
 *
 * Two members of this list carry a word from the prohibited decision vocabulary:
 * `BACKPRESSURE_REJECT` and `AUTH_ORIGIN_DENIED`. Both are VERBATIM wire values from the
 * `WsErrorCode` enum in `contracts/openapi.yaml` (lines 411–414) and the close-code table in
 * technical-design.md §2.5, which Pair A owns. Renaming either one client-side would make a real
 * capacity refusal or a real origin failure unrecognizable in a cross-language comparison, which is
 * worse than the word. Both describe transport-layer outcomes — queue capacity (rules.md R-20) and
 * handshake provenance — and neither is ever a statement about a caller. Neither may be reused as
 * action vocabulary (rules.md R-07): the four control steps live in `ACTIONS` below and nowhere else.
 * Named here rather than blended, per rules.md R-54; a repository vocabulary check needs these two
 * strings on its exemption list, scoped to this file and to `lib/stream.ts`.
 */
export const WS_ERROR_CODES = [
  'AUTH_TICKET_MISSING',
  'AUTH_TICKET_INVALID',
  'AUTH_ORIGIN_DENIED',
  'PROTO_FRAME_SIZE',
  'PROTO_SEQUENCE',
  'PROTO_FIRST_MESSAGE',
  'PROTO_PURPOSE_MISMATCH',
  'PROTO_PAYLOAD_TOO_LARGE',
  'SESSION_ALREADY_STREAMING',
  'BACKPRESSURE_REJECT',
  'SCORER_UNAVAILABLE',
] as const;
export type WsErrorCode = (typeof WS_ERROR_CODES)[number];

/** `CreateSessionRequest.client_call_ref.maxLength` in `contracts/openapi.yaml`. */
export const MAX_CALL_REF_LENGTH = 128;

/** `CreateSessionResponse.call_ref` / `SessionOpen.call_ref` pattern: 64 lowercase hex characters. */
const CALL_REF_PATTERN = /^[0-9a-f]{64}$/;

// --- REST bodies ---------------------------------------------------------------------------------

export interface CreateSessionRequest {
  /**
   * The raw, human-readable demo reference. This is the only field in the entire client that ever
   * holds it, it crosses the wire exactly once, and it is never persisted, logged, or placed in a
   * URL (rules.md R-16). The Gateway HMACs it on receipt and returns only the pseudonym.
   */
  client_call_ref: string;
  purpose_code: PurposeCode;
  context_value_band: ContextValueBand;
  /** Asserts the notice was displayed AND acknowledged before capture (rules.md R-18). */
  consent_acknowledged: true;
}

export interface CreateSessionResponse {
  session_id: string;
  /** HMAC-SHA256 pseudonym, hex. The only caller reference that exists downstream. */
  call_ref: string;
  purpose_code: PurposeCode;
  context_value_band: ContextValueBand;
  policy_version: string;
  retention_days: number;
  expires_at: string;
  /**
   * Returned by the Gateway beyond the published schema. Optional here so a Gateway build that
   * predates them still type-checks; when `probability_language_permitted` is false the UI must not
   * describe `spoof_risk` with probability language (rules.md R-11).
   */
  artifact_state?: ArtifactState;
  probability_language_permitted?: boolean;
}

export interface StreamTicketResponse {
  /**
   * Short-lived, single-use, bound to `session_id` + `sub` (decision D-6). Not a secret to store —
   * a credential to spend. It is never written to storage, never placed in a URL, and never logged
   * (rules.md R-34).
   */
  ticket: string;
  /** Offer this VERBATIM as the second WebSocket subprotocol. Pre-assembled by the Gateway. */
  subprotocol: string;
  expires_in_seconds: number;
}

/** The parity set from `GET /api/v1/version` (architecture.md §5.1). */
export interface VersionInfo {
  git_commit: string;
  deployment_profile: DeploymentProfile;
  execution_provider?: string;
  api_schema_sha256?: string;
  proto_sha256?: string;
  policy_version?: string;
  policy_bundle_sha256?: string;
  model_version?: string;
  model_sha256?: string;
  calibration_version?: string;
  calibration_sha256?: string;
  migration_head?: string;
  detector_mode?: DetectorMode;
  artifact_state: ArtifactState;
}

/** Feature-only audit event record from `GET /api/v1/sessions/{id}/audit`. */
export interface AuditEventRecord {
  event_id: string;
  tenant_id?: string;
  session_id: string;
  call_ref: string;
  event_seq: number;
  occurred_at: string;
  purpose_code: PurposeCode;
  context_value_band: ContextValueBand;
  window_seq: number | null;
  spoof_risk: number | null;
  risk_state: RiskState;
  action: Action;
  reason_code: ReasonCode | string;
  policy_version: string;
  policy_bundle_sha256: string;
  model_version: string;
  model_sha256: string;
  calibration_version: string;
  calibration_sha256: string;
  quality_flags: QualityFlag[];
  detector_mode: DetectorMode | string;
  execution_provider: string;
  deployment_profile: DeploymentProfile | string;
  prev_event_hash: string;
  event_hash: string;
  retention_expires_at: string;
}

/** Verification and chained event listing response from `GET /api/v1/sessions/{id}/audit`. */
export interface SessionAuditResponse {
  session_id: string;
  chain_verified: boolean;
  first_divergent_event_seq: number | null;
  events: AuditEventRecord[];
  phase_note?: string;
}

// --- WSS messages --------------------------------------------------------------------------------

/**
 * The first and only text frame the client sends (technical-design.md §2.2).
 *
 * `additionalProperties: false` is enforced server-side, so an extra field here is a closed
 * connection rather than an ignored key. That strictness is a privacy control: a tolerated unknown
 * field is somewhere a transcript or a phone number could ride in on the audio channel.
 */
export interface SessionOpen {
  type: 'session.open';
  call_ref: string;
  purpose_code: PurposeCode;
  context_value_band: ContextValueBand;
  client_capture: ClientCapture;
}

export interface ClientCapture {
  sample_rate_hz: number;
  frame_ms: number;
  /**
   * `scriptprocessor` is the honest current state; `audioworklet` is the target
   * (architecture.md §3). Declared on the wire so the current-state honesty required by
   * rules.md R-01 is visible at runtime and not only in a document.
   */
  path: 'scriptprocessor' | 'audioworklet';
}

export interface SessionAccepted {
  type: 'session.accepted';
  session_id: string;
  policy_version: string;
  model_version?: string;
  calibration_version?: string;
  deployment_profile: DeploymentProfile;
  execution_provider?: string;
  detector_mode?: DetectorMode;
  artifact_state?: ArtifactState;
}

export interface RiskEvent {
  type: 'risk.event';
  window_seq: number;
  /** Calibrated, `[0,1]`. Not proof of anything about a person. */
  spoof_risk: number;
  risk_state: RiskState;
  /**
   * `false` means the window is SKIPPED by the k-of-n count — absence of evidence, never evidence
   * of absence (rules.md R-09). `RiskTimeline` must not draw it on the same magnitude scale as a
   * counted window.
   */
  eligible: boolean;
  quality_flags: QualityFlag[];
  occurred_at: string;
}

export interface PolicyAction {
  type: 'policy.action';
  action: Action;
  risk_state: RiskState;
  purpose_code: PurposeCode;
  policy_version: string;
  reason_code: ReasonCode;
  audit_event_id?: string;
  /** Eligible windows counted (n) and high windows among them (k). */
  evidence_window_count?: number;
  evidence_high_count?: number;
}

export interface WsError {
  type: 'error';
  code: WsErrorCode;
  /**
   * Present on the wire and DELIBERATELY UNUSED by the UI. Server text is never rendered and never
   * interpolated into a user-facing string (rules.md R-17); the client keeps its own static copy
   * keyed by `code`. Kept in the type so nobody "adds" it later thinking it was overlooked.
   */
  message?: string;
}

export interface SessionClosed {
  type: 'session.closed';
  reason_code: string;
  windows_scored?: number;
  /** The Gateway asserting the volatile PCM ring buffer was zeroed (rules.md R-14). */
  buffer_cleared: true;
}

export type ServerEvent = SessionAccepted | RiskEvent | PolicyAction | WsError | SessionClosed;

// --- Pure validators -----------------------------------------------------------------------------

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function member<T extends string>(values: readonly T[], candidate: unknown): T | null {
  return typeof candidate === 'string' && (values as readonly string[]).includes(candidate)
    ? (candidate as T)
    : null;
}

export function isPurposeCode(value: unknown): value is PurposeCode {
  return member(PURPOSE_CODES, value) !== null;
}

export function isContextValueBand(value: unknown): value is ContextValueBand {
  return member(CONTEXT_VALUE_BANDS, value) !== null;
}

export function isCallRef(value: unknown): value is string {
  return typeof value === 'string' && CALL_REF_PATTERN.test(value);
}

/** Quality flags outside the schema are discarded rather than displayed as unknown labels. */
function knownQualityFlags(value: unknown): QualityFlag[] {
  if (!Array.isArray(value)) return [];
  const flags: QualityFlag[] = [];
  for (const candidate of value) {
    const flag = member(QUALITY_FLAGS, candidate);
    if (flag !== null) flags.push(flag);
  }
  return flags;
}

/**
 * Parse one server text frame into a validated `ServerEvent`, or `null`.
 *
 * `null` covers malformed JSON, an unrecognized `type`, and a recognized `type` whose required
 * fields are missing or out of range. Dropping is the correct response to all three: a `risk.event`
 * with `spoof_risk` of `NaN` or `1.4` rendered as evidence would be the UI inventing a measurement,
 * and that is the failure this whole component tree exists to avoid.
 */
export function parseServerEvent(raw: string): ServerEvent | null {
  let payload: unknown;
  try {
    payload = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!isRecord(payload)) return null;

  switch (payload['type']) {
    case 'session.accepted':
      return parseSessionAccepted(payload);
    case 'risk.event':
      return parseRiskEvent(payload);
    case 'policy.action':
      return parsePolicyAction(payload);
    case 'error':
      return parseWsError(payload);
    case 'session.closed':
      return parseSessionClosed(payload);
    default:
      return null;
  }
}

function parseSessionAccepted(payload: Record<string, unknown>): SessionAccepted | null {
  const sessionId = payload['session_id'];
  const policyVersion = payload['policy_version'];
  const profile = member(['aws-gpu', 'local-cpu'] as const, payload['deployment_profile']);
  if (typeof sessionId !== 'string' || typeof policyVersion !== 'string' || profile === null) {
    return null;
  }
  return {
    type: 'session.accepted',
    session_id: sessionId,
    policy_version: policyVersion,
    deployment_profile: profile,
    model_version: optionalString(payload['model_version']),
    calibration_version: optionalString(payload['calibration_version']),
    execution_provider: optionalString(payload['execution_provider']),
    detector_mode:
      member(['REAL_DETECTOR', 'MOCK_SMOKE_MODE_NOT_A_DETECTOR'] as const, payload['detector_mode']) ??
      undefined,
    artifact_state:
      member(['research_only', 'demo_eligible', 'policy_eligible'] as const, payload['artifact_state']) ??
      undefined,
  };
}

function parseRiskEvent(payload: Record<string, unknown>): RiskEvent | null {
  const windowSeq = payload['window_seq'];
  const spoofRisk = payload['spoof_risk'];
  const state = member(RISK_STATES, payload['risk_state']);
  const eligible = payload['eligible'];
  const occurredAt = payload['occurred_at'];

  if (typeof windowSeq !== 'number' || !Number.isInteger(windowSeq) || windowSeq < 0) return null;
  // Range is checked, not clamped. A score outside [0,1] is not a display problem to smooth over —
  // it means the calibration contract was violated upstream (rules.md R-24).
  if (typeof spoofRisk !== 'number' || !Number.isFinite(spoofRisk) || spoofRisk < 0 || spoofRisk > 1) {
    return null;
  }
  if (state === null || typeof eligible !== 'boolean' || typeof occurredAt !== 'string') return null;

  return {
    type: 'risk.event',
    window_seq: windowSeq,
    spoof_risk: spoofRisk,
    risk_state: state,
    eligible,
    quality_flags: knownQualityFlags(payload['quality_flags']),
    occurred_at: occurredAt,
  };
}

function parsePolicyAction(payload: Record<string, unknown>): PolicyAction | null {
  const action = member(ACTIONS, payload['action']);
  const state = member(RISK_STATES, payload['risk_state']);
  const purpose = member(PURPOSE_CODES, payload['purpose_code']);
  const reason = member(REASON_CODES, payload['reason_code']);
  const policyVersion = payload['policy_version'];

  // An unrecognized action is dropped, never shown. A banner that renders whatever string arrived
  // would be a one-message route around the closed vocabulary in rules.md R-07.
  if (action === null || state === null || purpose === null || reason === null) return null;
  if (typeof policyVersion !== 'string') return null;

  return {
    type: 'policy.action',
    action,
    risk_state: state,
    purpose_code: purpose,
    policy_version: policyVersion,
    reason_code: reason,
    audit_event_id: optionalString(payload['audit_event_id']),
    evidence_window_count: optionalCount(payload['evidence_window_count']),
    evidence_high_count: optionalCount(payload['evidence_high_count']),
  };
}

function parseWsError(payload: Record<string, unknown>): WsError | null {
  const code = member(WS_ERROR_CODES, payload['code']);
  if (code === null) return null;
  // `message` is intentionally not copied across. See the comment on `WsError.message`.
  return { type: 'error', code };
}

function parseSessionClosed(payload: Record<string, unknown>): SessionClosed | null {
  const reason = payload['reason_code'];
  if (typeof reason !== 'string' || payload['buffer_cleared'] !== true) return null;
  return {
    type: 'session.closed',
    reason_code: reason,
    windows_scored: optionalCount(payload['windows_scored']),
    buffer_cleared: true,
  };
}

function optionalString(value: unknown): string | undefined {
  return typeof value === 'string' && value.length > 0 ? value : undefined;
}

function optionalCount(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : undefined;
}
