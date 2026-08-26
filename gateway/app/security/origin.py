"""Origin allow-list — PURE, no I/O (rules.md R-53).

Browsers set ``Origin`` on a WebSocket handshake and will not let page script forge it, which makes
this the one cross-site control available on the WSS upgrade. CORS does not apply to WebSockets, so
without this check any page on the internet could open an authenticated stream against the Gateway
using a ticket it phished.

Exact string match only. Not a suffix match, not a regex, not a wildcard: ``endswith(".example.com")``
matches ``evil-example.com``, and every wildcard origin check that has ever shipped was written by
someone who knew that and thought their pattern was the safe one.
"""

from __future__ import annotations

from typing import Iterable


class OriginDenied(Exception):
    """``Origin`` header missing or not on the allow-list."""

    __slots__ = ("code",)

    def __init__(self) -> None:
        super().__init__("AUTH_ORIGIN_DENIED")
        self.code = "AUTH_ORIGIN_DENIED"


def normalize_origin(value: str) -> str:
    """Lower-case scheme+host and drop a default port.

    ``https://example.com`` and ``https://example.com:443`` are the same origin per RFC 6454, and a
    client library may send either. Normalizing both sides means the allow-list can be written the
    obvious way without a spurious deny.
    """
    origin = value.strip().rstrip("/")
    for scheme, default_port in (("https://", ":443"), ("http://", ":80")):
        if origin.lower().startswith(scheme) and origin.endswith(default_port):
            origin = origin[: -len(default_port)]
    return origin.lower()


def check_origin(origin: str | None, allowed: Iterable[str]) -> str:
    """Return the normalized origin, or raise.

    An absent ``Origin`` is denied rather than allowed. A non-browser client can omit it, but a
    non-browser client is not the demo's traffic source, and treating "no origin" as trusted is how
    this control gets bypassed.
    """
    if not origin:
        raise OriginDenied()
    candidate = normalize_origin(origin)
    if candidate not in {normalize_origin(a) for a in allowed}:
        raise OriginDenied()
    return candidate
