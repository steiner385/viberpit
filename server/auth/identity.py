"""Tailscale identity resolution (T028).

Resolves the caller's verified identity from Tailscale-provided request headers.
Primary source: Tailscale-User-Login (set by `tailscale serve` proxy).
Fallback: X-Tailscale-User-Login (alternate header name some setups use).

Header trust is conditional on the PEER, not on the header's presence. `tailscale
serve` proxies to the edge's loopback bind, so a genuinely stamped header always
arrives from loopback (or from an address listed in CCSWITCH_TRUSTED_PROXY_IPS).
Presented by anyone else it is an unverified claim and is discarded — the request
then falls through to `tailscale whois`, which verifies the peer against the local
tailscaled rather than believing what the caller wrote.

Allowlist: if Settings.tailnet_allowlist is non-empty, identity must be in the
list or None is returned (deny). If empty (dev default), any identity is accepted.
"""
from __future__ import annotations

import ipaddress
import logging
import subprocess
from typing import Union
from starlette.requests import Request
from starlette.websockets import WebSocket

from server.config import get_settings

logger = logging.getLogger(__name__)

_AnyAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]


def host_is_loopback(host: str | None) -> bool:
    """True when *host* is a loopback address — the one such check in the codebase.

    Shared with the assistant-token gate in ``server/auth/middleware.py``, which
    imports it from here rather than keeping a second copy.

    Loopback is the whole ``127.0.0.0/8`` block plus ``::1``, so this asks
    ``ipaddress`` rather than comparing against a literal set. One wrinkle: for the
    IPv4-mapped form a dual-stack listener reports (``::ffff:127.0.0.1``) ipaddress
    returns ``is_loopback=False``, so the mapped v4 address is unwrapped first.

    Fails closed: a host that is not an IP address at all (Starlette's TestClient
    default, ``"testclient"``) is not loopback.
    """
    if not host:
        return False
    try:
        addr: _AnyAddress = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return addr.is_loopback


# Synthetic identity handed to unidentified clients when LAN-open mode is enabled
# (CCSWITCH_ALLOW_UNAUTHENTICATED). Recognisable in logs/session authorship as a
# non-Tailscale, deliberately-unauthenticated caller.
OPEN_LAN_IDENTITY = "lan@local"


def verify_identity(request: Request | WebSocket) -> str | None:
    """Return the verified identity string, or None if unverifiable/denied.

    Accepts a ``Request`` or a ``WebSocket`` — only ``.headers`` is read, which
    both expose, so the same identity check covers HTTP and WS handshakes.
    """
    identity = (
        request.headers.get("Tailscale-User-Login")
        or request.headers.get("X-Tailscale-User-Login")
    )

    # The header is only evidence when the peer is entitled to stamp it. From anyone
    # else it is an unverified claim, so drop it and carry on as if it were absent —
    # `whois` below is peer-verified and may still identify the caller, and LAN-open
    # mode must still be able to admit them as a guest. Denying outright here would
    # turn a stray header into a lockout.
    if identity and not _peer_is_trusted(request):
        # The asserted value is deliberately NOT logged: it is either attacker-chosen
        # text or a real person's email address, and neither belongs in a log line
        # (server/audit.py redacts token material, but not identities). The peer is
        # the actionable datum — it says who tried.
        peer = request.client.host if request.client else "<unknown>"
        logger.warning(
            "ignoring Tailscale identity header asserted by untrusted peer %s "
            "(trusted: loopback + CCSWITCH_TRUSTED_PROXY_IPS)",
            peer,
        )
        identity = None

    if not identity:
        # Best-effort: try `tailscale whois` on the peer address
        identity = _whois_identity(request)

    if not identity:
        # No Tailscale identity. In LAN-open mode (opt-in, off by default) admit the
        # caller as an unauthenticated guest rather than denying. The allowlist below
        # is intentionally NOT applied to the guest — this branch IS the "no auth" path.
        if get_settings().allow_unauthenticated:
            return OPEN_LAN_IDENTITY
        return None

    # Allowlist check (real, presented identities only — unaffected by LAN-open mode)
    settings = get_settings()
    allowlist = settings.tailnet_allowlist_items
    if allowlist and identity not in allowlist:
        return None

    return identity


def _peer_is_trusted(request: Request | WebSocket) -> bool:
    """True when this peer may assert an identity header.

    Fails closed: a request with no peer address (ASGI ``client`` absent) cannot be
    placed on either side of the boundary, so it does not get to assert one.
    """
    peer = request.client.host if request.client else None
    if not peer:
        return False
    if host_is_loopback(peer):
        return True
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for entry in get_settings().trusted_proxy_ips_items:
        try:
            # strict=False so a host address ("10.4.4.4") is accepted alongside a
            # range ("10.0.0.0/24"); a malformed entry is skipped, never fatal —
            # one typo in the list must not take the whole gate down.
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            logger.warning("ignoring malformed CCSWITCH_TRUSTED_PROXY_IPS entry %r", entry)
            continue
        if addr in network:
            return True
    return False


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
