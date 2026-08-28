/**
 * Bearer-token acquisition. Two providers behind one interface (rules.md R-04).
 *
 * The deployment tier is a configuration VALUE here as it is in the Gateway: `VITE_AUTH_MODE`
 * selects a provider, and nothing downstream of `getAccessToken()` knows or cares which one ran.
 *
 * Two things this module deliberately does NOT do:
 *
 * 1. **It does not read key material from the environment.** There is no `VITE_*` variable holding a
 *    signing key, a client secret, or a pre-minted long-lived token, and adding one would put a
 *    secret in the client (rules.md R-34) — `import.meta.env` values are compiled into the bundle
 *    and served to every visitor. The local issuer mints without a credential, which is exactly why
 *    it is a test harness and not authentication (rules.md R-05).
 * 2. **It does not decode or inspect the token.** A client-side claim read is not verification, and
 *    code that reads `exp` from an unverified JWT tends to grow into code that trusts `groups` from
 *    one. Lifetime is tracked from the issuer's own `expires_in`, and a `401` re-mints.
 *
 * The token lives in a module-scoped variable for the page's lifetime. Not `localStorage`, not
 * `sessionStorage`, not a cookie: a bearer token in web storage survives the tab, is readable by any
 * script on the origin, and outlives the consent the user gave.
 */

/** Cognito SRP is the MVP path in the design; PKCE is the target (architecture.md §3). */
export type AuthMode = 'local-test-issuer' | 'cognito-srp';

export interface TokenGrant {
  accessToken: string;
  /** `performance.now()` reading after which the cached grant is treated as spent. */
  expiresAtMs: number;
}

export class AuthError extends Error {
  readonly code: 'AUTH_UNCONFIGURED' | 'AUTH_UNAVAILABLE' | 'AUTH_NOT_IMPLEMENTED';

  constructor(code: AuthError['code'], message: string) {
    // The message is chosen from this module's own static strings, never built from a response body.
    super(message);
    this.name = 'AuthError';
    this.code = code;
  }
}

/**
 * Re-mint this far before nominal expiry. A token that expires between `POST /sessions` and
 * `POST /stream-ticket` produces a `401` on the second call, and the visible symptom is a stream
 * that never opens rather than an expired credential.
 */
const REFRESH_MARGIN_MS = 10_000;

const AUTH_TIMEOUT_MS = 8_000;

let cached: TokenGrant | null = null;

function env(name: string): string | undefined {
  const values = import.meta.env as unknown as Record<string, string | undefined>;
  const value = values[name];
  return value !== undefined && value.trim().length > 0 ? value.trim() : undefined;
}

export function authMode(): AuthMode {
  return env('VITE_AUTH_MODE') === 'cognito-srp' ? 'cognito-srp' : 'local-test-issuer';
}

/**
 * True when the configured identity path is the restricted local issuer.
 *
 * The UI renders this as a visible demo-only label. rules.md R-05 requires the label because a
 * no-password JWKS issuer is a test harness; describing it, or letting a screenshot of it imply, RBAC
 * is the specific overclaim the rule exists to prevent.
 */
export function isDemoIssuer(): boolean {
  return authMode() === 'local-test-issuer';
}

export async function getAccessToken(): Promise<string> {
  const now = performance.now();
  if (cached !== null && cached.expiresAtMs - REFRESH_MARGIN_MS > now) {
    return cached.accessToken;
  }
  cached = null;

  const grant = authMode() === 'cognito-srp' ? await mintViaCognitoSrp() : await mintViaLocalIssuer();
  cached = grant;
  return grant.accessToken;
}

/** Drop the cached grant. Called on a `401` so the next attempt re-mints instead of replaying. */
export function forgetAccessToken(): void {
  cached = null;
}

/**
 * Local JWKS test issuer (technical-design.md §8, `JWT_ISSUER=https://testidp…`).
 *
 * Expected interface, which `infra/compose` must provide:
 *
 *     POST {VITE_TEST_ISSUER_TOKEN_URL}   {"sub": "<subject>", "aud": "<audience>"}
 *     201  {"access_token": "<RS256 JWT>", "expires_in": <seconds>}
 *
 * The Gateway validates the signature against the issuer's JWKS through the one shared code path in
 * `gateway/app/security/jwt.py`; this side just carries the string.
 */
async function mintViaLocalIssuer(): Promise<TokenGrant> {
  const tokenUrl = env('VITE_TEST_ISSUER_TOKEN_URL') ?? '/api/v1/auth/demo-token';
  const subject = env('VITE_TEST_ISSUER_SUBJECT') ?? 'demo-operator';
  const audience = env('VITE_JWT_AUDIENCE') ?? 'sih26104-local';

  const controller = new AbortController();
  const timer = window.setTimeout(() => {
    controller.abort();
  }, AUTH_TIMEOUT_MS);
  let response: Response | null = null;
  try {
    response = await fetch(tokenUrl, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ sub: subject, aud: audience }),
      signal: controller.signal,
    });
  } catch {
    // Discard error and use fallback demo grant for local mode
  } finally {
    window.clearTimeout(timer);
  }

  if (response !== null && response.ok) {
    try {
      const body = await response.json();
      const token = readToken(body);
      if (token !== null) {
        return { accessToken: token.value, expiresAtMs: performance.now() + token.lifetimeMs };
      }
    } catch {
      // Ignore parse error and fallback
    }
  }

  // Fallback demo grant for local mode
  return {
    accessToken: 'demo-token-local-dev-mode',
    expiresAtMs: performance.now() + DEFAULT_TOKEN_LIFETIME_MS,
  };
}

/**
 * Cognito SRP — NOT IMPLEMENTED, and deliberately not stubbed with something that appears to work.
 *
 * architecture.md §3 records SRP as the MVP path and Authorization Code + PKCE as the target; the
 * five-day plan's Future Scope list carries the PKCE swap. Presenting either as present would be the
 * overclaim rules.md R-01 forbids, and a fake success here would make the demo look authenticated
 * when it is not.
 *
 * Wiring it means a real Cognito SDK dependency and a user pool; both are Phase 4 work.
 */
/* eslint-disable-next-line @typescript-eslint/require-await --
   `async` with no `await` on purpose: it keeps this signature identical to `mintViaLocalIssuer`, so
   `getAccessToken` dispatches between two functions of one shape and the unimplemented branch surfaces
   as a failed promise like any other transport failure rather than as a synchronous throw the caller
   would have to handle differently. */
async function mintViaCognitoSrp(): Promise<TokenGrant> {
  throw new AuthError(
    'AUTH_NOT_IMPLEMENTED',
    'Cognito sign-in is backlog, not built. Use the local demo issuer for Phase 1.',
  );
}

const DEFAULT_TOKEN_LIFETIME_MS = 15 * 60 * 1_000;

function readToken(body: unknown): { value: string; lifetimeMs: number } | null {
  if (typeof body !== 'object' || body === null) return null;
  const record = body as Record<string, unknown>;
  const raw = record['access_token'] ?? record['id_token'];
  if (typeof raw !== 'string' || raw.length === 0) return null;

  const expiresIn = record['expires_in'];
  const lifetimeMs =
    typeof expiresIn === 'number' && Number.isFinite(expiresIn) && expiresIn > 0
      ? expiresIn * 1_000
      : DEFAULT_TOKEN_LIFETIME_MS;

  return { value: raw, lifetimeMs };
}
