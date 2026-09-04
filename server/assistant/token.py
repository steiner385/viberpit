"""Assistant bearer token: identity + session bound, HMAC-signed with the app session secret,
accepted ONLY from loopback by IdentityMiddleware (spec §4.3). Never persisted."""
from __future__ import annotations

import json
from dataclasses import dataclass

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from server.auth.middleware import _signing_secret

PREFIX = "ccas1:"
_SALT = "ccswitch-assistant-v1"


@dataclass(frozen=True)
class AssistantClaims:
    identity: str
    session_id: str


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_signing_secret(), salt=_SALT)


def mint_token(identity: str, session_id: str) -> str:
    payload = json.dumps({"i": identity, "s": session_id}, separators=(",", ":"))
    return PREFIX + _serializer().dumps(payload)


def verify_token(token: str, *, max_age_s: float) -> AssistantClaims | None:
    if not token.startswith(PREFIX):
        return None
    try:
        raw = _serializer().loads(token[len(PREFIX):], max_age=int(max_age_s))
        data = json.loads(raw)
        return AssistantClaims(identity=str(data["i"]), session_id=str(data["s"]))
    except (BadSignature, SignatureExpired, ValueError, KeyError, TypeError):
        return None
