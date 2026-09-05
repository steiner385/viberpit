"""Tailscale identity resolution (T028).

Resolves the caller's verified identity from Tailscale-provided request headers.
Primary source: Tailscale-User-Login (set by `tailscale serve` proxy).
Fallback: X-Tailscale-User-Login (alternate header name some setups use).

Allowlist: if Settings.tailnet_allowlist is non-empty, identity must be in the
list or None is returned (deny). If empty (dev default), any identity is accepted.
"""
from __future__ import annotations

import ipaddress
import subprocess
from starlette.requests import Request
from starlette.websockets import WebSocket

from server.config import get_settings

# Synthetic identity handed to unidentified clients when LAN-open mode is enabled
# (CCSWITCH_ALLOW_UNAUTHENTICATED). Recognisable in logs/session authorship as a
# non-Tailscale, deliberately-unauthenticated caller.
OPEN_LAN_IDENTITY = "lan@local"


def _peer_is_trusted(request: Request | WebSocket, settings) -> bool:
    """Return True if the immediate peer may assert an identity header.

    The Tailscale-User-Login header is only trustworthy when stamped by
    `tailscale serve` (or an operator-declared trusted proxy) sitting directly
    in front of this process — anything else can present an arbitrary
    identity and walk straight through the allowlist (issue #1131). The
    allowlist constrains *which* identity is accepted, not *who* may assert
    one, so that check alone is not a substitute for this one.

    A peer host that fails to parse as an IP address (e.g. the literal string
    "testclient" that Starlette's TestClient uses as its default peer) cannot
    occur on a real network connection — every real ASGI server populates
    ``scope["client"]`` from the actual socket peer address, never a
    hostname. Treating it as trusted only affects in-process test transports,
    not attacker-reachable traffic.
    """
    client = getattr(request, "client", None)
    peer = client.host if client else None
    if not peer:
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return True
    if addr.is_loopback:
        return True
    for entry in settings.trusted_proxy_ip_items:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        if addr in network:
            return True
    return False


def verify_identity(request: Request | WebSocket) -> str | None:
    """Return the verified identity string, or None if unverifiable/denied.

    Accepts a ``Request`` or a ``WebSocket`` — only ``.headers``/``.client``
    are read, which both expose, so the same identity check covers HTTP and
    WS handshakes.
    """
    settings = get_settings()
    header_identity = (
        request.headers.get("Tailscale-User-Login")
        or request.headers.get("X-Tailscale-User-Login")
    )

    identity: str | None
    if header_identity:
        if not _peer_is_trusted(request, settings):
            # Fail closed: a header from an untrusted peer is a spoofing
            # attempt, not a caller to fall back to whois or the guest path.
            return None
        identity = header_identity
    else:
        # Best-effort: try `tailscale whois` on the peer address
        identity = _whois_identity(request)

    if not identity:
        # No Tailscale identity. In LAN-open mode (opt-in, off by default) admit the
        # caller as an unauthenticated guest rather than denying. The allowlist below
        # is intentionally NOT applied to the guest — this branch IS the "no auth" path.
        if settings.allow_unauthenticated:
            return OPEN_LAN_IDENTITY
        return None

    # Allowlist check (real, presented identities only — unaffected by LAN-open mode)
    allowlist = settings.tailnet_allowlist_items
    if allowlist and identity not in allowlist:
        return None

    return identity


def _whois_identity(request: Request | WebSocket) -> str | None:
    """Try to resolve identity via `tailscale whois <peer-ip>`.

    Wraps the subprocess call in try/except so any failure returns None gracefully.
    This is a best-effort fallback; the header path is preferred.
    """
    import json
    try:
        peer_ip = request.client.host if request.client else None
        if not peer_ip:
            return None
        result = subprocess.run(
            ["tailscale", "whois", "--json", peer_ip],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return None
        # tailscale whois --json → {"UserProfile": {"LoginName": "user@example.com", ...}}
        data = json.loads(result.stdout)
        login = (data.get("UserProfile") or {}).get("LoginName")
        return login or None
    except Exception:
        pass
    return None
