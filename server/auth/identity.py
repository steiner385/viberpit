"""Tailscale identity resolution (T028).

Resolves the caller's verified identity from Tailscale-provided request headers.
Primary source: Tailscale-User-Login (set by `tailscale serve` proxy).
Fallback: X-Tailscale-User-Login (alternate header name some setups use).

Header trust is conditional on the PEER, not on the header's presence. `tailscale
serve` reaches the edge from this host, but its backend hop is sourced from the
host's OWN tailnet address rather than 127.0.0.1 (#2134), so a genuinely stamped
header arrives from loopback, from one of this node's own tailscale addresses, or
from an address listed in VIBERPIT_TRUSTED_PROXY_IPS.
Presented by anyone else it is an unverified claim and is discarded — the request
then falls through to `tailscale whois`, which verifies the peer against the local
tailscaled rather than believing what the caller wrote.

Allowlist: if Settings.tailnet_allowlist is non-empty, identity must be in the
list or None is returned (deny). If empty (dev default), any identity is accepted.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import subprocess
import time
from typing import Union
from starlette.requests import Request
from starlette.websockets import WebSocket

from server.config import get_settings
from server.obs.logdedupe import LogThrottle, log_throttled

logger = logging.getLogger(__name__)

_AnyAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

# A single untrusted peer (or a proxy hop whose IP dropped out of
# VIBERPIT_TRUSTED_PROXY_IPS / the self-tailscale-IP cache) re-enters this branch on
# every request it sends, turning one standing misconfiguration into tens of
# thousands of identical WARNINGs (#1483: 63729 occurrences from one peer over ~40h).
# server.obs.errsink files one auto-error issue per fingerprint, so the flood buried
# the "still happening" signal under its own repeats. Keyed by peer IP: cardinality
# is bounded by LogThrottle's own eviction, and a newly-untrusted peer is still worth
# reporting promptly even while another one's window is open.
_UNTRUSTED_IDENTITY_HEADER_LOG = LogThrottle()

# This node's own tailnet addresses are stable, so a hit is cached for minutes. A
# miss (tailscale absent / daemon down) is cached briefly so a host without
# tailscale does not pay a subprocess spawn per request, yet recovers quickly.
_SELF_IPS_TTL_S = 300.0
_SELF_IPS_MISS_TTL_S = 30.0
_self_ips_cache: tuple[float, frozenset[_AnyAddress]] | None = None


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
# (VIBERPIT_ALLOW_UNAUTHENTICATED). Recognisable in logs/session authorship as a
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
        log_throttled(
            logger,
            _UNTRUSTED_IDENTITY_HEADER_LOG,
            peer,
            logging.WARNING,
            "ignoring Tailscale identity header asserted by untrusted peer %s "
            "(trusted: loopback + this node's own tailnet IPs + VIBERPIT_TRUSTED_PROXY_IPS)",
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


def _self_tailscale_ips() -> frozenset[_AnyAddress]:
    """This machine's own tailnet addresses, per ``tailscale status --json``.

    ``tailscale serve``'s backend hop is sourced from the host's own tailnet
    interface address, not loopback (#2134) — so a request genuinely proxied by
    ``tailscale serve`` on this box arrives with the peer set to one of these. A
    miss (binary absent, daemon down, non-tailscale host) yields an empty set,
    which trusts nothing extra; it never raises. Cached (see ``_SELF_IPS_TTL_S``)
    since this rarely changes and a subprocess spawn per request is the exact cost
    this fix removes from the ``whois`` fallback path.
    """
    global _self_ips_cache
    now = time.monotonic()
    if _self_ips_cache is not None:
        expires_at, cached_ips = _self_ips_cache
        if now < expires_at:
            return cached_ips
    resolved_ips: frozenset[_AnyAddress] = frozenset()
    ttl = _SELF_IPS_MISS_TTL_S
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            raw = (data.get("Self") or {}).get("TailscaleIPs") or []
            parsed = set()
            for value in raw:
                try:
                    parsed.add(ipaddress.ip_address(value))
                except ValueError:
                    continue
            resolved_ips = frozenset(parsed)
            ttl = _SELF_IPS_TTL_S
    except Exception:
        pass
    _self_ips_cache = (now + ttl, resolved_ips)
    return resolved_ips


def reset_self_ips_cache() -> None:
    """Drop the memoised self-IP set.

    This is process-global state, so the suite resets it around every test (autouse
    fixture in ``server/tests/conftest.py``, alongside the helper-call breakers and
    the gh-credential latch). A test that stubs ``subprocess.run`` would otherwise
    freeze its fake answer here for the whole TTL and silently change what every
    LATER test on that xdist worker sees — an order-dependent failure that looks
    nothing like its cause.
    """
    global _self_ips_cache
    _self_ips_cache = None


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
    if addr in _self_tailscale_ips():
        return True
    for entry in get_settings().trusted_proxy_ips_items:
        try:
            # strict=False so a host address ("10.4.4.4") is accepted alongside a
            # range ("10.0.0.0/24"); a malformed entry is skipped, never fatal —
            # one typo in the list must not take the whole gate down.
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            logger.warning("ignoring malformed VIBERPIT_TRUSTED_PROXY_IPS entry %r", entry)
            continue
        if addr in network:
            return True
    return False


def _whois(request: Request | WebSocket) -> dict | None:
    """Run `tailscale whois --json` against the request's verified peer address.

    Returns the parsed JSON document, or None on any failure. This is the ONLY
    subprocess wrapper for whois: identity (LoginName) and client locality
    (Node.ComputedName) are both projections over it, so they can never disagree
    about which peer was asked.
    """
    import json
    try:
        direct_peer = request.client.host if request.client else None
        peer_ip: str | None = direct_peer
        forwarded = request.headers.get("X-Real-IP")
        # Only honour X-Real-IP when something entitled to set it sits in front:
        # loopback, one of this node's own tailnet addresses, or an address in
        # VIBERPIT_TRUSTED_PROXY_IPS. This is the same predicate the identity-header
        # gate uses — one peer-trust rule in this module, not two that can drift
        # apart. Evaluated only when there IS a forwarded address for it to gate:
        # with no X-Real-IP the verdict cannot change peer_ip, and since #2134 the
        # check can cost a `tailscale status` spawn — the exact per-request cost
        # this whole path exists to stop paying.
        if forwarded and _peer_is_trusted(request):
            peer_ip = forwarded
        if not peer_ip:
            return None
        # Validate peer_ip is a real IP address to prevent argv flag injection
        # (e.g. a value like "--flag" being misinterpreted by tailscale whois).
        try:
            ipaddress.ip_address(peer_ip)
        except ValueError:
            return None
        result = subprocess.run(
            ["tailscale", "whois", "--json", "--", peer_ip],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _whois_identity(request: Request | WebSocket) -> str | None:
    """Try to resolve identity via `tailscale whois <peer-ip>`.

    Best-effort fallback; the header path is preferred.
    tailscale whois --json → {"UserProfile": {"LoginName": "user@example.com", ...}}
    """
    data = _whois(request)
    if not data:
        return None
    login = (data.get("UserProfile") or {}).get("LoginName")
    return login or None


def client_node_name(request: Request | WebSocket) -> str | None:
    """The tailnet machine the caller's browser is on, or None if unknowable.

    ``Node.ComputedName`` is the MagicDNS short name ("dobby"), which is also the
    first label of the kernel hostname and therefore what ``server.hostname
    .self_host_name()`` returns on that machine — the comparison the display
    router makes. Lower-cased so the comparison is case-insensitive. Never raises;
    a loopback peer with no forwarded address has no whois entry and resolves to
    None, so a local ``curl 127.0.0.1`` never masquerades as the operator's desktop.
    """
    data = _whois(request)
    if not data:
        return None
    name = (data.get("Node") or {}).get("ComputedName")
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip().lower()
