"""THE exposure policy (#1410, FR-G12.1/.2): one tier setting, validated fail-closed.

``VIBERPIT_EXPOSURE`` names how far the edge is reachable, and each tier names what it
requires before the edge may start (constitution IV: network identity may gate a
private deployment; app-level strong authentication is required before any non-private
exposure; no deployment is ever reachable by an unidentified caller):

=========  ==========================  =================================================
Tier       Bind (``VIBERPIT_HOST``)    Requires
=========  ==========================  =================================================
loopback   a loopback address          —
tailnet    loopback or a tailnet IP    a tailnet allowlist; a configured browser origin
lan        any private address         everything in tailnet, plus passkeys enabled with
                                       at least one registered credential, a persistent
                                       cookie-signing secret, and Secure cookies
public     any                         everything in lan, plus ``VIBERPIT_SESSION_SECRET``
                                       itself, an https ``VIBERPIT_PUBLIC_ORIGIN`` and the
                                       operator's attestation that the ingress sends HSTS
=========  ==========================  =================================================

Passkeys, when enabled in any tier, also need a configured origin: the WebAuthn relying
party is derived from it (``server/auth/webauthn.py``).

A violated rule is a ``RuntimeError`` naming the tier, the rule and the fix.
``VIBERPIT_DEV=1`` relaxes only ``loopback``/``tailnet`` violations, to one warning; it
never relaxes ``lan`` or ``public``. The "at least one credential" rule reads the
database, so it runs separately (:func:`validate_exposure_credentials`) once the DB is up.
"""
from __future__ import annotations

import ipaddress
import logging
import os

from server.auth.identity import TAILNET_NETS, host_is_loopback
from server.auth.origin import configured_origins, normalize_origin
from server.config import get_settings

log = logging.getLogger(__name__)

TIERS = ("loopback", "tailnet", "lan", "public")
#: Tiers whose violations ``VIBERPIT_DEV=1`` may downgrade to a warning.
DEV_RELAXABLE = frozenset({"loopback", "tailnet"})
#: Tiers reachable by devices that carry no tailnet identity: passkeys are the gate.
PASSKEY_TIERS = frozenset({"lan", "public"})

_warned_dev: set[str] = set()


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _bind_is_tailnet(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in net for net in TAILNET_NETS)


def _bind_is_private(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (addr.is_private or _bind_is_tailnet(host)) and not addr.is_unspecified


def tier() -> str:
    """The configured tier, lower-cased (validated by :func:`exposure_violations`)."""
    return (get_settings().exposure or "").strip().lower()


def exposure_violations() -> list[tuple[str, str]]:
    """Every ``(rule, fix)`` the current settings break for the configured tier."""
    settings = get_settings()
    name = tier()
    if name not in TIERS:
        return [(f"unknown tier {name!r}", f"set VIBERPIT_EXPOSURE to one of {', '.join(TIERS)}")]
    host = settings.host or ""
    out: list[tuple[str, str]] = []
    origins = configured_origins(include_dev=False)

    if name == "loopback":
        if not host_is_loopback(host):
            out.append((f"bind {host!r} is not a loopback address",
                        "set VIBERPIT_HOST=127.0.0.1, or choose a wider VIBERPIT_EXPOSURE"))
    elif name == "tailnet":
        if not (host_is_loopback(host) or _bind_is_tailnet(host)):
            out.append((f"bind {host!r} is neither loopback nor a tailnet address",
                        "bind 127.0.0.1 behind tailscale serve/nginx, or set "
                        "VIBERPIT_EXPOSURE=lan"))
    elif name == "lan":
        if not (host_is_loopback(host) or _bind_is_private(host)):
            out.append((f"bind {host!r} is not a private address",
                        "bind a private address, or set VIBERPIT_EXPOSURE=public"))

    if name != "loopback":
        if not settings.tailnet_allowlist_items:
            out.append(("empty tailnet allowlist",
                        "set VIBERPIT_TAILNET_ALLOWLIST to the identities allowed in"))
        if not origins:
            out.append(("no configured browser origin",
                        "set VIBERPIT_PUBLIC_ORIGIN or VIBERPIT_ALLOWED_ORIGINS"))

    if name in PASSKEY_TIERS:
        if not settings.webauthn_enabled:
            out.append(("passkeys are disabled",
                        "set VIBERPIT_WEBAUTHN_ENABLED=true and enrol a passkey over the "
                        "tailnet first"))
        if not (settings.session_secret or settings.master_key):
            out.append(("no persistent cookie-signing secret",
                        "set VIBERPIT_SESSION_SECRET (or VIBERPIT_MASTER_KEY)"))
        if _env_true("VIBERPIT_DEV_INSECURE_COOKIE"):
            out.append(("VIBERPIT_DEV_INSECURE_COOKIE drops the Secure cookie flag",
                        "unset VIBERPIT_DEV_INSECURE_COOKIE and serve over https"))

    if name == "public":
        if not settings.session_secret:
            out.append(("VIBERPIT_SESSION_SECRET is not set",
                        "set a dedicated VIBERPIT_SESSION_SECRET"))
        public = normalize_origin(settings.public_origin)
        if public is None or not public.startswith("https://"):
            out.append(("no https VIBERPIT_PUBLIC_ORIGIN",
                        "set VIBERPIT_PUBLIC_ORIGIN=https://<public host>"))
        if not _env_true("VIBERPIT_INGRESS_HSTS"):
            out.append(("HSTS at the ingress is not attested",
                        "send Strict-Transport-Security at the ingress, then set "
                        "VIBERPIT_INGRESS_HSTS=true"))

    if settings.webauthn_enabled and not origins:
        rule = ("passkeys need a relying-party origin",
                "set VIBERPIT_PUBLIC_ORIGIN or VIBERPIT_ALLOWED_ORIGINS")
        if rule[0] not in {r for r, _ in out}:
            out.append(rule)
    return out


def validate_exposure() -> None:
    """Refuse to start (``RuntimeError``) when the configured tier's rules are broken."""
    name = tier()
    violations = exposure_violations()
    if not violations:
        return
    message = "; ".join(f"exposure={name}: {rule} — fix: {fix}" for rule, fix in violations)
    if name in DEV_RELAXABLE and os.environ.get("VIBERPIT_DEV") == "1":
        if message not in _warned_dev:
            _warned_dev.add(message)
            log.warning("SECURITY (VIBERPIT_DEV=1, not enforced): %s", message)
        return
    raise RuntimeError(f"refusing to start: {message}")


async def validate_exposure_credentials(db) -> None:
    """The database half: a passkey tier needs at least one live credential, or no
    device without Tailscale could ever sign in."""
    name = tier()
    if name not in PASSKEY_TIERS:
        return
    from server.auth.webauthn import live_credential_count  # noqa: PLC0415
    if await live_credential_count(db) < 1:
        raise RuntimeError(
            f"refusing to start: exposure={name}: no registered passkey — fix: enrol one "
            "over the tailnet (Settings › Security) with VIBERPIT_EXPOSURE=tailnet first")
