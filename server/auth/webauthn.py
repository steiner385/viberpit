"""Passkeys (WebAuthn): THE app-level strong-authentication path (#1410, FR-G12.6–.11).

Constitution IV requires an enable-able strong-auth path before any non-private
exposure; this is it. The ceremonies are py_webauthn's (``webauthn`` on PyPI) — nothing
cryptographic is hand-rolled here. This module owns:

* the relying party: its id is the host of the primary configured origin, and the
  expected origins are ``server.auth.origin.configured_origins()`` — the Origin
  allowlist's own list, never a second one;
* single-use, short-lived ceremony challenges (held in this edge process: a restart
  only abandons ceremonies in flight);
* credential rows (``WebAuthnCredential``: public keys only) and the passkey
  ``AppSession`` a successful login mints. For such a session the signed cookie IS the
  authenticator (:func:`resolve_passkey_session`), with the 24 h absolute expiry plus
  an idle timeout (``VIBERPIT_SESSION_IDLE_S``).

Bootstrapping (FR-G12.8): a passkey is bound to the identity string the tailnet path
produces, so the allowlist governs both. Only a full-scope caller already signed in as
that identity — over the tailnet, or with one of its passkeys — and attested as a person
(``identity.human_sender``) may enrol one. Recovery (FR-G12.11) is the tailnet path:
sign in over Tailscale, revoke the lost passkey, enrol a new one.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import (
    base64url_to_bytes,
    bytes_to_base64url,
    parse_authentication_credential_json,
)
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidJSONStructure,
    InvalidRegistrationResponse,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from viberpit import brand
from server.auth.identity import identity_allowed
from server.auth.origin import configured_origins, normalize_origin
from server.config import get_settings
from server.db.clock import naive_utc_now
from server.db.models import AppSession, WebAuthnCredential

PASSKEY = "passkey"
#: How long a begun ceremony may wait for its completion.
CEREMONY_TTL_S = 300.0
#: Cap on ceremonies in flight, so unauthenticated login/begin cannot grow memory.
_MAX_CEREMONIES = 1024
#: Passkey session lifetime (absolute); the idle timeout is VIBERPIT_SESSION_IDLE_S.
SESSION_TTL = timedelta(hours=24)


class PasskeyError(Exception):
    """A refused ceremony. ``status`` is the HTTP status the route answers with."""

    def __init__(self, status: int, code: str, message: str, *, audit: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.audit = audit or code.lower()


# ---------------------------------------------------------------------------
# Relying party
# ---------------------------------------------------------------------------


def expected_origins() -> list[str]:
    """The origins an assertion may come from: the Origin allowlist's own list."""
    return sorted(configured_origins())


def rp_id() -> str:
    """Host of the primary configured origin: ``VIBERPIT_PUBLIC_ORIGIN``, else the first
    ``VIBERPIT_ALLOWED_ORIGINS`` entry, else the first configured origin (dev)."""
    settings = get_settings()
    candidates = [settings.public_origin or ""]
    candidates += [o.strip() for o in (settings.allowed_origins or "").split(",")]
    for raw in candidates:
        norm = normalize_origin(raw)
        if norm:
            return _host(norm)
    origins = expected_origins()
    if not origins:
        raise PasskeyError(503, "PASSKEY_NO_ORIGIN",
                           "set VIBERPIT_PUBLIC_ORIGIN or VIBERPIT_ALLOWED_ORIGINS")
    return _host(origins[0])


def _host(origin: str) -> str:
    netloc = origin.split("://", 1)[1]
    if netloc.startswith("["):
        return netloc[1:netloc.index("]")]
    return netloc.split(":", 1)[0]


# ---------------------------------------------------------------------------
# Ceremony challenges: server-side, single-use, short TTL
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Ceremony:
    kind: str                 # "register" | "login"
    challenge: bytes
    identity: str | None      # the enrolling identity; None for a usernameless login
    expires_at: float


_ceremonies: dict[str, _Ceremony] = {}


def _open_ceremony(kind: str, challenge: bytes, identity: str | None) -> str:
    now = time.monotonic()
    for key in [k for k, c in _ceremonies.items() if c.expires_at <= now]:
        del _ceremonies[key]
    if len(_ceremonies) >= _MAX_CEREMONIES:
        raise PasskeyError(429, "PASSKEY_BUSY", "too many sign-ins in progress; retry shortly")
    ceremony_id = secrets.token_urlsafe(24)
    _ceremonies[ceremony_id] = _Ceremony(kind, challenge, identity, now + CEREMONY_TTL_S)
    return ceremony_id


def _take_ceremony(ceremony_id: str, kind: str) -> _Ceremony:
    """Pop the ceremony (single use: a replay finds nothing) or refuse with 400."""
    ceremony = _ceremonies.pop(str(ceremony_id or ""), None)
    if ceremony is None or ceremony.kind != kind or ceremony.expires_at <= time.monotonic():
        raise PasskeyError(400, "PASSKEY_CEREMONY_INVALID",
                           "this sign-in has expired or was already used; start again",
                           audit="ceremony_invalid")
    return ceremony


def reset_ceremonies() -> None:
    """Drop every pending ceremony (test isolation)."""
    _ceremonies.clear()


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def _user_handle(identity: str) -> bytes:
    """Stable, opaque WebAuthn user id: never the identity itself (an email)."""
    return hashlib.sha256(f"{brand.SLUG}:{identity}".encode()).digest()[:32]


async def live_credentials(db: AsyncSession, identity: str) -> list[WebAuthnCredential]:
    return list((await db.execute(
        select(WebAuthnCredential)
        .where(WebAuthnCredential.identity == identity, WebAuthnCredential.revoked_at.is_(None))
        .order_by(WebAuthnCredential.created_at)
    )).scalars())


async def live_credential_count(db: AsyncSession) -> int:
    return int((await db.execute(
        select(func.count()).select_from(WebAuthnCredential)
        .where(WebAuthnCredential.revoked_at.is_(None))
    )).scalar_one())


def may_enrol(*, identity: str | None, scope: str | None, auth_method: str | None,
              human: bool) -> bool:
    """FR-G12.8: full scope, signed in as *identity* over the tailnet or with one of its
    passkeys, on the allowlist, and a person (not an agent process on this host)."""
    return bool(identity) and (scope or "full") == "full" \
        and auth_method in ("tailscale", PASSKEY) and human \
        and identity_allowed(identity or "")


async def begin_registration(db: AsyncSession, identity: str) -> dict:
    existing = await live_credentials(db, identity)
    challenge = secrets.token_bytes(32)
    options = generate_registration_options(
        rp_id=rp_id(),
        rp_name=brand.NAME,
        user_name=identity,
        user_id=_user_handle(identity),
        user_display_name=identity,
        challenge=challenge,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id))
            for c in existing],
    )
    return {"ceremonyId": _open_ceremony("register", challenge, identity),
            "publicKey": _options(options)}


async def complete_registration(db: AsyncSession, identity: str, ceremony_id: str,
                                credential: dict, label: str | None) -> WebAuthnCredential:
    ceremony = _take_ceremony(ceremony_id, "register")
    if ceremony.identity != identity:
        raise PasskeyError(400, "PASSKEY_CEREMONY_INVALID",
                           "this enrolment was started by someone else",
                           audit="ceremony_identity_mismatch")
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=ceremony.challenge,
            expected_rp_id=rp_id(),
            expected_origin=expected_origins(),
            require_user_verification=True,
        )
    except (InvalidRegistrationResponse, InvalidJSONStructure, ValueError, KeyError,
            TypeError) as exc:
        raise PasskeyError(400, "PASSKEY_REGISTRATION_INVALID",
                           "the passkey could not be verified",
                           audit="registration_invalid") from exc
    transports = (credential.get("response") or {}).get("transports") or []
    known = {t.value for t in AuthenticatorTransport}
    row = WebAuthnCredential(
        identity=identity,
        credential_id=bytes_to_base64url(verified.credential_id),
        public_key=bytes_to_base64url(verified.credential_public_key),
        sign_count=int(verified.sign_count),
        transports=[t for t in transports if isinstance(t, str) and t in known] or None,
        label=(label or "").strip()[:80] or None,
        created_at=naive_utc_now(),
    )
    db.add(row)
    return row


async def begin_authentication(db: AsyncSession, identity: str | None) -> dict:
    """Login options. With *identity*, its passkeys are listed; without, the browser
    offers any discoverable passkey for this relying party (usernameless)."""
    allow: list[PublicKeyCredentialDescriptor] = []
    if identity:
        allow = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id))
                 for c in await live_credentials(db, identity)]
    challenge = secrets.token_bytes(32)
    options = generate_authentication_options(
        rp_id=rp_id(), challenge=challenge, allow_credentials=allow,
        user_verification=UserVerificationRequirement.REQUIRED)
    return {"ceremonyId": _open_ceremony("login", challenge, identity or None),
            "publicKey": _options(options)}


async def complete_authentication(db: AsyncSession, ceremony_id: str,
                                  credential: dict) -> tuple[WebAuthnCredential, AppSession]:
    """Verify an assertion and mint the passkey session. Raises :class:`PasskeyError`."""
    ceremony = _take_ceremony(ceremony_id, "login")
    try:
        parsed = parse_authentication_credential_json(credential)
    except (InvalidJSONStructure, ValueError, KeyError, TypeError) as exc:
        raise PasskeyError(401, "PASSKEY_REJECTED", "that passkey was not accepted",
                           audit="assertion_malformed") from exc
    row = (await db.execute(
        select(WebAuthnCredential).where(
            WebAuthnCredential.credential_id == bytes_to_base64url(parsed.raw_id))
    )).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        raise PasskeyError(401, "PASSKEY_REJECTED", "that passkey was not accepted",
                           audit="credential_unknown_or_revoked")
    if ceremony.identity is not None and ceremony.identity != row.identity:
        raise PasskeyError(401, "PASSKEY_REJECTED", "that passkey was not accepted",
                           audit="credential_identity_mismatch")
    if not identity_allowed(row.identity):
        raise PasskeyError(401, "PASSKEY_REJECTED", "that passkey was not accepted",
                           audit="identity_not_allowlisted")
    try:
        verified = verify_authentication_response(
            credential=parsed,
            expected_challenge=ceremony.challenge,
            expected_rp_id=rp_id(),
            expected_origin=expected_origins(),
            credential_public_key=base64url_to_bytes(row.public_key),
            credential_current_sign_count=int(row.sign_count),
            require_user_verification=True,
        )
    except InvalidAuthenticationResponse as exc:
        audit = "sign_count_regression" if "sign count" in str(exc).lower() \
            else "assertion_invalid"
        raise PasskeyError(401, "PASSKEY_REJECTED", "that passkey was not accepted",
                           audit=audit) from exc
    now = naive_utc_now()
    row.sign_count = int(verified.new_sign_count)
    row.last_used_at = now
    session = AppSession(
        identity=row.identity, created_at=now, expires_at=now + SESSION_TTL,
        revoked_at=None, scope="full", auth_method=PASSKEY, credential_id=row.id,
        last_seen_at=now)
    db.add(session)
    return row, session


async def revoke_credential(db: AsyncSession, identity: str, credential_id: str) -> bool:
    """FR-G12.11: revoke one of *identity*'s passkeys and every session it minted."""
    row = (await db.execute(
        select(WebAuthnCredential).where(
            WebAuthnCredential.id == credential_id, WebAuthnCredential.identity == identity,
            WebAuthnCredential.revoked_at.is_(None))
    )).scalar_one_or_none()
    if row is None:
        return False
    now = naive_utc_now()
    row.revoked_at = now
    await db.execute(
        update(AppSession)
        .where(AppSession.credential_id == row.id, AppSession.revoked_at.is_(None))
        .values(revoked_at=now))
    return True


def credential_summary(row: WebAuthnCredential) -> dict:
    return {
        "id": row.id,
        "label": row.label,
        "transports": list(row.transports or []),
        "createdAt": _iso(row.created_at),
        "lastUsedAt": _iso(row.last_used_at),
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value is not None else None


def _options(options) -> dict:
    import json  # noqa: PLC0415
    return json.loads(options_to_json(options))


# ---------------------------------------------------------------------------
# The passkey session: the cookie is the authenticator
# ---------------------------------------------------------------------------


async def resolve_passkey_session(db: AsyncSession, session_id: str) -> AppSession | None:
    """The LIVE passkey session *session_id*, touched; else None.

    Live = not revoked, inside the 24 h absolute expiry, touched within
    ``VIBERPIT_SESSION_IDLE_S``, its credential not revoked, and its identity still on
    the allowlist. An idle-expired session is revoked so it cannot be revived.
    """
    now = naive_utc_now()
    row = (await db.execute(
        select(AppSession).where(
            AppSession.id == session_id, AppSession.auth_method == PASSKEY,
            AppSession.revoked_at.is_(None), AppSession.expires_at > now)
    )).scalar_one_or_none()
    if row is None:
        return None
    idle = timedelta(seconds=max(1, int(get_settings().session_idle_s)))
    seen = row.last_seen_at or row.created_at
    credential = None
    if row.credential_id:
        credential = (await db.execute(
            select(WebAuthnCredential).where(WebAuthnCredential.id == row.credential_id)
        )).scalar_one_or_none()
    if now - seen > idle or credential is None or credential.revoked_at is not None:
        row.revoked_at = now
        return None
    if not identity_allowed(row.identity):
        return None
    row.last_seen_at = now
    return row


def not_passkey():
    """SQL predicate: an ambient (tailnet) session row, never a passkey one — the
    ambient path must not adopt a session whose cookie authenticates on its own."""
    return or_(AppSession.auth_method.is_(None), AppSession.auth_method != PASSKEY)
