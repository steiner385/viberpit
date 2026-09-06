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


USER_PREFIX = "ccas2:"
_USER_SALT = "ccswitch-assistant-user-v1"


@dataclass(frozen=True)
class UserClaims:
    identity: str
    scope: str
    token_id: str


def _user_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_signing_secret(), salt=_USER_SALT)


def mint_user_token(identity: str, scope: str, token_id: str) -> str:
    """Mint a `ccas2:` bearer token. The signature alone is not authoritative — the
    caller (IdentityMiddleware) still looks up `token_id` in `AssistantToken` to honour
    revocation and the per-token `expires_at`, both of which live in the DB rather than
    the signed payload so a token can be revoked without waiting for it to expire."""
    payload = json.dumps({"i": identity, "s": scope, "t": token_id}, separators=(",", ":"))
    return USER_PREFIX + _user_serializer().dumps(payload)


def verify_user_token(token: str, *, max_age_s: float) -> UserClaims | None:
    if not token.startswith(USER_PREFIX):
        return None
    try:
        raw = _user_serializer().loads(token[len(USER_PREFIX):], max_age=int(max_age_s))
        data = json.loads(raw)
        return UserClaims(identity=str(data["i"]), scope=str(data["s"]), token_id=str(data["t"]))
    except (BadSignature, SignatureExpired, ValueError, KeyError, TypeError):
        return None
