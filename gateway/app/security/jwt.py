"""JWT validation — ONE code path for both tiers (rules.md R-04).

Cognito on AWS and the restricted local JWKS test issuer differ by *configuration only*: issuer,
audience, and JWKS URL. There is no ``if profile == "aws"`` branch here, which is what makes the
local tier a parity fallback rather than a second, less-tested implementation.

The local issuer is a **demo test harness, not authentication** (rules.md R-05,
research-evidence.md correction 3). ``config.Settings`` refuses to start when it is configured under
``aws-gpu``; this module does not need to know which one it is talking to.

Validation rules that are non-negotiable:

* Algorithms are pinned to RS256 from configuration. The token's own ``alg`` header never selects
  the verification algorithm — that is the ``alg: none`` / HS256-confusion family of bug.
* ``iss`` and ``aud`` are both verified. A signature-only check accepts a valid token minted for a
  different application.
* JWKS is fetched over TLS, cached, and refreshed on unknown ``kid`` with a floor between refreshes,
  so an attacker cannot turn "unknown kid" into an unbounded outbound request amplifier.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Final

import httpx
from jose import jwt
from jose.exceptions import JOSEError

_ALGORITHMS: Final[tuple[str, ...]] = ("RS256",)
_JWKS_CACHE_SECONDS: Final[int] = 600
_JWKS_MIN_REFRESH_SECONDS: Final[int] = 30
_JWKS_TIMEOUT_SECONDS: Final[float] = 3.0


class AuthError(Exception):
    """Token rejected.

    One message for every cause. A caller learns "unauthorized", never which check failed, because
    distinguishing them turns this into a validation oracle.
    """

    def __init__(self) -> None:
        super().__init__("unauthorized")


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller, reduced to what the Gateway actually needs.

    ``sub`` binds stream tickets; ``groups`` is read for Phase-4 role checks. No email, no name, no
    phone number is retained — the audit table has no column for any of them, and carrying a value
    the schema forbids invites it into a log line.
    """

    sub: str
    groups: tuple[str, ...] = ()


class JwksCache:
    """Fetches and caches a JWKS document.

    Takes its clock and HTTP client by injection so token-validation tests need neither a network
    nor a sleep.
    """

    __slots__ = ("_client", "_clock", "_fetched_at", "_keys", "_last_attempt", "_url")

    def __init__(
        self,
        jwks_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Any = time.monotonic,
    ):
        self._url = jwks_url
        self._client = client
        self._clock = clock
        self._keys: dict[str, dict[str, Any]] = {}
        self._fetched_at = 0.0
        self._last_attempt = 0.0

    async def get(self, kid: str) -> dict[str, Any]:
        now = self._clock()
        fresh = (now - self._fetched_at) < _JWKS_CACHE_SECONDS

        if kid in self._keys and fresh:
            return self._keys[kid]

        # Unknown kid, or the cache aged out. Rate-limit the refresh so a stream of tokens with
        # random kids cannot be used to hammer the issuer through us.
        if (now - self._last_attempt) >= _JWKS_MIN_REFRESH_SECONDS or not self._keys:
            await self._refresh(now)

        key = self._keys.get(kid)
        if key is None:
            raise AuthError()
        return key

    async def _refresh(self, now: float) -> None:
        self._last_attempt = now
        client = self._client or httpx.AsyncClient(timeout=_JWKS_TIMEOUT_SECONDS)
        try:
            response = await client.get(self._url)
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AuthError() from exc
        finally:
            if self._client is None:
                await client.aclose()

        keys = {k["kid"]: k for k in document.get("keys", []) if "kid" in k}
        if not keys:
            raise AuthError()
        self._keys = keys
        self._fetched_at = now


class TokenValidator:
    """Validates bearer tokens against one issuer. Constructed once per process."""

    __slots__ = ("_audience", "_issuer", "_jwks", "_leeway")

    def __init__(self, *, issuer: str, audience: str, jwks: JwksCache, leeway_seconds: int = 30):
        self._issuer = issuer
        self._audience = audience
        self._jwks = jwks
        self._leeway = leeway_seconds

    async def validate(self, token: str) -> Principal:
        """Verify signature, issuer, audience, and expiry; return the principal.

        Raises:
            AuthError: on any failure.
        """
        if not token or token.count(".") != 2:
            raise AuthError()

        try:
            header = jwt.get_unverified_header(token)
        except JOSEError as exc:
            raise AuthError() from exc

        kid = header.get("kid")
        if not kid or header.get("alg") not in _ALGORITHMS:
            # Reject an unexpected alg before fetching a key. The token header is untrusted input;
            # it is a routing hint at most, and never the algorithm choice.
            raise AuthError()

        key = await self._jwks.get(str(kid))

        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=list(_ALGORITHMS),  # pinned by config, never read from the token
                issuer=self._issuer,
                audience=self._audience,
                options={
                    "verify_signature": True,
                    "verify_aud": True,
                    "verify_iss": True,
                    "verify_exp": True,
                    "leeway": self._leeway,
                },
            )
        except JOSEError as exc:
            raise AuthError() from exc

        sub = claims.get("sub")
        if not sub:
            raise AuthError()

        raw_groups = claims.get("cognito:groups") or claims.get("groups") or ()
        groups = tuple(str(g) for g in raw_groups) if isinstance(raw_groups, (list, tuple)) else ()
        return Principal(sub=str(sub), groups=groups)


def bearer_from_header(value: str | None) -> str:
    """Extract the token from an ``Authorization`` header."""
    if not value:
        raise AuthError()
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthError()
    return token.strip()
