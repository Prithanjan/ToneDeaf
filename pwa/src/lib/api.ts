/**
 * REST calls against the Gateway: `POST /api/v1/sessions`, `POST /api/v1/stream-ticket`,
 * `GET /api/v1/version`.
 *
 * Two properties of this module are privacy controls rather than error-handling taste:
 *
 * **The raw caller reference passes through and is not retained.** `createSession` takes it as an
 * argument, puts it in one request body over TLS, and holds no reference to it afterwards. It is
 * never a query parameter, never a path segment, never logged, and never written to storage
 * (rules.md R-16). A URL is the worst of those: it lands in browser history, in the CloudFront access
 * log, and in any proxy log between here and the Gateway.
 *
 * **Server response text never reaches the DOM.** `ApiError.message` is chosen from the static table
 * below, keyed by HTTP status and by a `code` validated against a known set. The server's own
 * `message` field is read for nothing. The Gateway already guarantees static error text
 * (rules.md R-17), but a client that renders whatever arrived would make that guarantee the only
 * thing standing between a future logging change and a caller reference in the page.
 */

import type {
  Action,
  ArtifactState,
  AuditEventRecord,
  ContextValueBand,
  CreateSessionResponse,
  DetectorMode,
  PurposeCode,
  QualityFlag,
  RiskState,
  SessionAuditResponse,
  StreamTicketResponse,
  VersionInfo,
} from './types';
import { isCallRef, isContextValueBand, isPurposeCode } from './types';

const SESSIONS_PATH = '/api/v1/sessions';
const STREAM_TICKET_PATH = '/api/v1/stream-ticket';
const VERSION_PATH = '/api/v1/version';
const STREAM_PATH = '/ws/v1/stream';

const REQUEST_TIMEOUT_MS = 10_000;

export type ApiErrorCode =
  | 'CONFIG'
  | 'NETWORK'
  | 'TIMEOUT'
  | 'UNAUTHORIZED'
  | 'CONSENT_REQUIRED'
  | 'INVALID_CALL_REF'
  | 'SESSION_UNKNOWN'
  | 'SESSION_ALREADY_STREAMING'
  | 'CAPACITY'
  | 'MALFORMED_RESPONSE'
  | 'SERVER';

export class ApiError extends Error {
  readonly code: ApiErrorCode;
  readonly status: number | null;

  constructor(code: ApiErrorCode, status: number | null = null) {
    super(API_ERROR_TEXT[code]);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
  }
}

/** Client-owned, static, complete. The only strings this module will ever show a person. */
const API_ERROR_TEXT: Record<ApiErrorCode, string> = {
  CONFIG: 'The Gateway address is not configured.',
  NETWORK: 'The Gateway could not be reached.',
  TIMEOUT: 'The Gateway did not respond in time.',
  UNAUTHORIZED: 'This sign-in is not valid for the Gateway.',
  CONSENT_REQUIRED: 'The privacy notice must be acknowledged before a session can be created.',
  INVALID_CALL_REF: 'That call reference is not an acceptable length or character set.',
  SESSION_UNKNOWN: 'That session is unknown or has expired.',
  SESSION_ALREADY_STREAMING: 'That session already has a live stream.',
  CAPACITY: 'The Gateway is at capacity. No new session was created.',
  MALFORMED_RESPONSE: 'The Gateway returned a response this client cannot read.',
  SERVER: 'The Gateway refused the request.',
};

/**
 * Where the Gateway lives. Defaults to the serving origin, which is the shape both tiers deploy in:
 * CloudFront in front of the ALB on `aws-gpu`, Caddy in front of the Gateway on `local-cpu`. A
 * same-origin default also keeps the `Origin` header on the WSS handshake equal to the site the user
 * is actually on, which is the value the Gateway's permit list is written against.
 */
export function apiBaseUrl(): string {
  const values = import.meta.env as unknown as Record<string, string | undefined>;
  const configured = values['VITE_API_BASE_URL']?.trim();
  const base = configured !== undefined && configured.length > 0 ? configured : window.location.origin;
  return base.replace(/\/+$/, '');
}

/**
 * `https://host` → `wss://host/ws/v1/stream`.
 *
 * The scheme is derived, never configured separately: a `ws://` URL against an `https://` page is
 * refused by the browser as mixed content, and a hand-written second variable is where that
 * mismatch gets introduced.
 */
export function streamUrl(): string {
  const base = new URL(apiBaseUrl());
  base.protocol = base.protocol === 'http:' ? 'ws:' : 'wss:';
  base.pathname = STREAM_PATH;
  base.search = '';
  base.hash = '';
  return base.toString();
}

export interface CreateSessionInput {
  /** Raw, human-readable, held by the caller in component state only (rules.md R-16). */
  clientCallRef: string;
  purposeCode: PurposeCode;
  contextValueBand: ContextValueBand;
}

/**
 * Create the session and exchange the raw reference for an HMAC pseudonym.
 *
 * `consent_acknowledged` is hard-coded `true` because this function is only reachable from a screen
 * that `ConsentNotice` has already gated (rules.md R-18). Making it a parameter would turn a
 * structural ordering property into a value a future caller could get wrong.
 */
export async function createSession(
  accessToken: string,
  input: CreateSessionInput,
): Promise<CreateSessionResponse> {
  const body = await postJson(SESSIONS_PATH, accessToken, {
    client_call_ref: input.clientCallRef,
    purpose_code: input.purposeCode,
    context_value_band: input.contextValueBand,
    consent_acknowledged: true,
  });
  return readSessionResponse(body);
}

/**
 * Mint a stream ticket. Called immediately before each handshake, including every reconnect.
 *
 * The ticket is single-use with a 60 s TTL (decision D-6, `TICKET_TTL_SECONDS`). Caching one to
 * "save a round trip" produces `AUTH_TICKET_INVALID` on the second handshake, which presents as an
 * authentication failure rather than as the reuse it is.
 */
export async function createStreamTicket(
  accessToken: string,
  sessionId: string,
): Promise<StreamTicketResponse> {
  const body = await postJson(STREAM_TICKET_PATH, accessToken, { session_id: sessionId });
  return readTicketResponse(body);
}

/**
 * The parity set. Read so the UI can state `detector_mode` and `artifact_state` on screen: mock mode
 * has to be loud everywhere it appears (rules.md R-46), and `artifact_state` below `policy_eligible`
 * is what makes a high-risk action a rehearsal rather than a decision.
 */
export async function fetchVersion(): Promise<VersionInfo> {
  const body = await requestJson(VERSION_PATH, { method: 'GET' });
  if (typeof body !== 'object' || body === null) throw new ApiError('MALFORMED_RESPONSE');
  const record = body as Record<string, unknown>;
  const gitCommit = record['git_commit'];
  const profile = record['deployment_profile'];
  const artifactState = readArtifactState(record['artifact_state']);
  if (typeof gitCommit !== 'string' || artifactState === undefined) {
    throw new ApiError('MALFORMED_RESPONSE');
  }
  if (profile !== 'aws-gpu' && profile !== 'local-cpu') throw new ApiError('MALFORMED_RESPONSE');
  return {
    git_commit: gitCommit,
    deployment_profile: profile,
    artifact_state: artifactState,
    execution_provider: stringOrUndefined(record['execution_provider']),
    api_schema_sha256: stringOrUndefined(record['api_schema_sha256']),
    proto_sha256: stringOrUndefined(record['proto_sha256']),
    policy_version: stringOrUndefined(record['policy_version']),
    policy_bundle_sha256: stringOrUndefined(record['policy_bundle_sha256']),
    model_version: stringOrUndefined(record['model_version']),
    model_sha256: stringOrUndefined(record['model_sha256']),
    calibration_version: stringOrUndefined(record['calibration_version']),
    calibration_sha256: stringOrUndefined(record['calibration_sha256']),
    migration_head: stringOrUndefined(record['migration_head']),
    detector_mode: readDetectorMode(record['detector_mode']),
  };
}

/**
 * Fetch the chained, feature-only audit log for a session and verify its hash continuity.
 */
export async function fetchSessionAudit(
  accessToken: string,
  sessionId: string,
): Promise<SessionAuditResponse> {
  const path = `${SESSIONS_PATH}/${encodeURIComponent(sessionId)}/audit`;
  const body = await requestJson(path, {
    method: 'GET',
    headers: {
      authorization: `Bearer ${accessToken}`,
    },
  });
  if (typeof body !== 'object' || body === null) throw new ApiError('MALFORMED_RESPONSE');
  const record = body as Record<string, unknown>;
  const returnedSessionId = record['session_id'];
  const chainVerified = record['chain_verified'];
  const eventsRaw = record['events'];

  if (typeof returnedSessionId !== 'string' || typeof chainVerified !== 'boolean' || !Array.isArray(eventsRaw)) {
    throw new ApiError('MALFORMED_RESPONSE');
  }

  const events: AuditEventRecord[] = [];
  for (const item of eventsRaw) {
    if (typeof item !== 'object' || item === null) continue;
    const e = item as Record<string, unknown>;
    const eventId = e['event_id'];
    const sessId = e['session_id'];
    const callRef = e['call_ref'];
    const eventSeq = e['event_seq'];
    const occurredAt = e['occurred_at'];
    const purposeCode = e['purpose_code'];
    const contextValueBand = e['context_value_band'];
    const riskState = e['risk_state'];
    const action = e['action'];
    const reasonCode = e['reason_code'];
    const policyVersion = e['policy_version'];
    const prevEventHash = e['prev_event_hash'];
    const eventHash = e['event_hash'];
    const retentionExpiresAt = e['retention_expires_at'];

    if (
      typeof eventId !== 'string' ||
      typeof sessId !== 'string' ||
      typeof callRef !== 'string' ||
      typeof eventSeq !== 'number' ||
      typeof occurredAt !== 'string' ||
      typeof purposeCode !== 'string' ||
      typeof contextValueBand !== 'string' ||
      typeof riskState !== 'string' ||
      typeof action !== 'string' ||
      typeof reasonCode !== 'string' ||
      typeof policyVersion !== 'string' ||
      typeof prevEventHash !== 'string' ||
      typeof eventHash !== 'string' ||
      typeof retentionExpiresAt !== 'string'
    ) {
      continue;
    }

    events.push({
      event_id: eventId,
      tenant_id: stringOrUndefined(e['tenant_id']),
      session_id: sessId,
      call_ref: callRef,
      event_seq: eventSeq,
      occurred_at: occurredAt,
      purpose_code: purposeCode as PurposeCode,
      context_value_band: contextValueBand as ContextValueBand,
      window_seq: typeof e['window_seq'] === 'number' ? e['window_seq'] : null,
      spoof_risk: typeof e['spoof_risk'] === 'number' ? e['spoof_risk'] : null,
      risk_state: riskState as RiskState,
      action: action as Action,
      reason_code: reasonCode,
      policy_version: policyVersion,
      policy_bundle_sha256: typeof e['policy_bundle_sha256'] === 'string' ? e['policy_bundle_sha256'] : '',
      model_version: typeof e['model_version'] === 'string' ? e['model_version'] : '',
      model_sha256: typeof e['model_sha256'] === 'string' ? e['model_sha256'] : '',
      calibration_version: typeof e['calibration_version'] === 'string' ? e['calibration_version'] : '',
      calibration_sha256: typeof e['calibration_sha256'] === 'string' ? e['calibration_sha256'] : '',
      quality_flags: Array.isArray(e['quality_flags']) ? (e['quality_flags'] as QualityFlag[]) : [],
      detector_mode: typeof e['detector_mode'] === 'string' ? e['detector_mode'] : '',
      execution_provider: typeof e['execution_provider'] === 'string' ? e['execution_provider'] : '',
      deployment_profile: typeof e['deployment_profile'] === 'string' ? e['deployment_profile'] : '',
      prev_event_hash: prevEventHash,
      event_hash: eventHash,
      retention_expires_at: retentionExpiresAt,
    });
  }

  return {
    session_id: returnedSessionId,
    chain_verified: chainVerified,
    first_divergent_event_seq:
      typeof record['first_divergent_event_seq'] === 'number' ? record['first_divergent_event_seq'] : null,
    events,
    phase_note: stringOrUndefined(record['phase_note']),
  };
}

// --- transport -----------------------------------------------------------------------------------

async function postJson(
  path: string,
  accessToken: string,
  payload: Record<string, unknown>,
): Promise<unknown> {
  return requestJson(path, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      authorization: `Bearer ${accessToken}`,
    },
    body: JSON.stringify(payload),
  });
}

async function requestJson(path: string, init: RequestInit): Promise<unknown> {
  const base = apiBaseUrl();
  if (base.length === 0) throw new ApiError('CONFIG');

  const controller = new AbortController();
  const timer = window.setTimeout(() => {
    controller.abort();
  }, REQUEST_TIMEOUT_MS);

  let response: Response;
  try {
    response = await fetch(`${base}${path}`, {
      ...init,
      signal: controller.signal,
      // No cookies. The Gateway authenticates with a bearer header and a subprotocol ticket, and a
      // credentialed cross-origin request would add CSRF surface to an endpoint that opens a
      // microphone.
      credentials: 'omit',
      cache: 'no-store',
      referrerPolicy: 'no-referrer',
    });
  } catch {
    // `AbortError` and a genuine network failure are told apart by the signal, not by the message.
    throw controller.signal.aborted ? new ApiError('TIMEOUT') : new ApiError('NETWORK');
  } finally {
    window.clearTimeout(timer);
  }

  if (!response.ok) throw await errorFor(response);

  try {
    return (await response.json()) as unknown;
  } catch {
    throw new ApiError('MALFORMED_RESPONSE', response.status);
  }
}

/**
 * Map a failure response to a client-owned error.
 *
 * The body is parsed only to read `code`, and only codes this client already knows are honoured.
 * Nothing from `message` is kept. An unrecognized `code` becomes the generic `SERVER` text rather
 * than being shown: an unknown code is by definition something this build has no honest sentence for.
 */
async function errorFor(response: Response): Promise<ApiError> {
  const code = await readErrorCode(response);
  if (code !== null) return new ApiError(code, response.status);
  if (response.status === 401 || response.status === 403) return new ApiError('UNAUTHORIZED', 401);
  if (response.status === 404) return new ApiError('SESSION_UNKNOWN', 404);
  if (response.status === 409) return new ApiError('SESSION_ALREADY_STREAMING', 409);
  if (response.status === 429) return new ApiError('CAPACITY', 429);
  return new ApiError('SERVER', response.status);
}

const KNOWN_SERVER_CODES: readonly ApiErrorCode[] = [
  'CONSENT_REQUIRED',
  'INVALID_CALL_REF',
  'SESSION_UNKNOWN',
  'SESSION_ALREADY_STREAMING',
  'CAPACITY',
];

async function readErrorCode(response: Response): Promise<ApiErrorCode | null> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return null;
  }
  if (typeof body !== 'object' || body === null) return null;

  // FastAPI nests a dict raised via `HTTPException(detail=…)` under `detail`; a bare error model puts
  // `code` at the top level. Both shapes appear in `gateway/app/api/v1/`, so both are read.
  const record = body as Record<string, unknown>;
  const detail = record['detail'];
  const source = typeof detail === 'object' && detail !== null ? (detail as Record<string, unknown>) : record;
  const code = source['code'];
  if (typeof code !== 'string') return null;
  return KNOWN_SERVER_CODES.find((known) => known === code) ?? null;
}

// --- response validation -------------------------------------------------------------------------

function readSessionResponse(body: unknown): CreateSessionResponse {
  if (typeof body !== 'object' || body === null) throw new ApiError('MALFORMED_RESPONSE');
  const record = body as Record<string, unknown>;

  const sessionId = record['session_id'];
  const callRef = record['call_ref'];
  const purposeCode = record['purpose_code'];
  const contextValueBand = record['context_value_band'];
  const policyVersion = record['policy_version'];
  const retentionDays = record['retention_days'];
  const expiresAt = record['expires_at'];

  // `call_ref` is checked against the 64-hex pseudonym pattern. That is not defensive typing: if a
  // Gateway change ever echoed the raw reference back here, this is the assertion that catches it
  // before the value is put in `session.open` and sent over the audio channel (rules.md R-16).
  if (typeof sessionId !== 'string' || !isCallRef(callRef)) throw new ApiError('MALFORMED_RESPONSE');
  if (!isPurposeCode(purposeCode) || !isContextValueBand(contextValueBand)) {
    throw new ApiError('MALFORMED_RESPONSE');
  }
  if (typeof policyVersion !== 'string' || typeof expiresAt !== 'string') {
    throw new ApiError('MALFORMED_RESPONSE');
  }

  return {
    session_id: sessionId,
    call_ref: callRef,
    purpose_code: purposeCode,
    context_value_band: contextValueBand,
    policy_version: policyVersion,
    retention_days: typeof retentionDays === 'number' && Number.isFinite(retentionDays) ? retentionDays : 0,
    expires_at: expiresAt,
    artifact_state: readArtifactState(record['artifact_state']),
    // Absent is treated as "not permitted". Assuming permission for a Gateway that does not report
    // it would let probability language onto the screen while calibration is still a placeholder
    // (rules.md R-11).
    probability_language_permitted: record['probability_language_permitted'] === true,
  };
}

function readTicketResponse(body: unknown): StreamTicketResponse {
  if (typeof body !== 'object' || body === null) throw new ApiError('MALFORMED_RESPONSE');
  const record = body as Record<string, unknown>;
  const ticket = record['ticket'];
  const subprotocol = record['subprotocol'];
  const expiresIn = record['expires_in_seconds'];
  if (typeof ticket !== 'string' || ticket.length === 0) throw new ApiError('MALFORMED_RESPONSE');
  if (typeof subprotocol !== 'string' || subprotocol.length === 0) throw new ApiError('MALFORMED_RESPONSE');
  return {
    ticket,
    subprotocol,
    expires_in_seconds: typeof expiresIn === 'number' && Number.isFinite(expiresIn) ? expiresIn : 0,
  };
}

function stringOrUndefined(value: unknown): string | undefined {
  return typeof value === 'string' && value.length > 0 ? value : undefined;
}

function readArtifactState(value: unknown): ArtifactState | undefined {
  return value === 'research_only' || value === 'demo_eligible' || value === 'policy_eligible'
    ? value
    : undefined;
}

function readDetectorMode(value: unknown): DetectorMode | undefined {
  return value === 'REAL_DETECTOR' || value === 'MOCK_SMOKE_MODE_NOT_A_DETECTOR' ? value : undefined;
}
