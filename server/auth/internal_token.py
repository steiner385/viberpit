"""Internal system bearer token: loopback-only, no human identity, HMAC-signed with
the app signing secret like the ``ccas1`` assistant token (server/assistant/token.py)
but bound to nothing — there is no session yet to bind to, and no identity beyond
"the primary daemon's own automation subsystem calling its co-located edge" (spec:
route automation session creation through the fleet-ranked FleetOrchestrator instead
of spawning unconditionally on the primary's own host). Never persisted, minted fresh
per call, short-lived: a captured token is worthless a few seconds later.
"""
from __future__ import annotations

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from server.auth.middleware import _cross_process_signing_secret

PREFIX = "ccsys1:"
_SALT = "viberpit-internal-system-v1"
# One HTTP round trip, never cached or reused — generous relative to normal latency
# but far short of a window an intercepted token could be replayed within.
MAX_AGE_S = 30.0

# The routes this token may call — unlike the assistant's ccas1 token (a whole
# allowlist for a human-facing drawer), this token has exactly one caller
# (server/sessions/internal_dispatch.py) and two jobs: place an automation session
# on the best-ranked host, and certify a harvested one-shot session `complete` on
# the host that owns it. ``ALLOWED_ROUTES`` is the single source of truth for the
# middleware's enforcement; the two path constants are what the router and the
# client share.
ALLOWED_METHOD = "POST"
ALLOWED_PATH = "/api/internal/automation-sessions"
CERTIFY_PATH = "/api/internal/certify-harvested"
ALLOWED_ROUTES: frozenset[tuple[str, str]] = frozenset({
    (ALLOWED_METHOD, ALLOWED_PATH),
    (ALLOWED_METHOD, CERTIFY_PATH),
})


def _serializer() -> URLSafeTimedSerializer:
    # Daemon-minted, edge-verified — same cross-process key rule as ccas1.
    return URLSafeTimedSerializer(_cross_process_signing_secret(), salt=_SALT)


def mint_token() -> str:
    return PREFIX + _serializer().dumps("system")


def verify_token(token: str) -> bool:
    if not token.startswith(PREFIX):
        return False
    try:
        _serializer().loads(token[len(PREFIX):], max_age=int(MAX_AGE_S))
        return True
    except (BadSignature, SignatureExpired, ValueError):
        return False
