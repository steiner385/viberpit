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
import os
import socket
import struct
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


# ``via`` for a caller that reached the edge from a process on THIS host rather than
# through ``tailscale serve``. Peer IP cannot tell the two apart (#2980): the serve hop
# and a session's ``curl`` both arrive from loopback / this node's tailnet address, and
# both may carry a ``Tailscale-User-Login`` header. What differs is who owns the client
# socket: tailscaled runs as root, an agent session runs as the operator's user.
LOCAL_PROCESS_VIA = "local"
# ``via`` for the same local caller when the operator opted in to trusting this host
# (``VIBERPIT_RULES_TRUST_LOCAL_HOST``, spec 2026-10-14): ``require_human`` admits it, and
# it is what the rule's ``created_via`` and the audit row record, so writes made under
# the opt-in stay distinguishable from attested ones.
LOCAL_TRUSTED_VIA = "local-trusted"
_PROC_NET_TCP = ("/proc/net/tcp", "/proc/net/tcp6")
# The two front-ends deploy/ ships, by the account their client socket belongs to, and
# the header each one stamps with the ORIGINAL client: tailscaled (`tailscale serve`,
# root) sets X-Forwarded-For; nginx's workers (deploy/viberpit-tailnet.nginx, www-data)
# overwrite X-Real-IP. A socket owned by anyone else on this host is a local process.
_FRONT_ENDS = (("root", "x-forwarded-for"), ("www-data", "x-real-ip"))
# /proc/net/tcp state for ESTABLISHED. A closed client socket turns into a timewait
# minisocket that the kernel prints with uid 0 and inode 0, so only a live, established
# socket says anything about its owner.
_TCP_ESTABLISHED = "01"
_UNATTRIBUTABLE_UID = -1


def _front_end_headers() -> dict[int, str]:
    """``{uid: header naming the original client}`` for the front-ends on this host."""
    import pwd  # noqa: PLC0415
    out: dict[int, str] = {}
    for name, header in _FRONT_ENDS:
        try:
            out[pwd.getpwnam(name).pw_uid] = header
        except KeyError:
            continue
    return out

# ``scope["client"]`` is NOT the socket peer: uvicorn's ProxyHeadersMiddleware rewrites
# it from ``X-Forwarded-For`` for any 127.0.0.1 caller (port included), which is how the
# loopback-only token gates in middleware.py keep serve-proxied tailnet traffic out. It
# also means a local process picks its own "peer". TransportPeerMiddleware records the
# real one under this key before anything rewrites it.
TRANSPORT_PEER_SCOPE_KEY = "viberpit.transport_peer"
# Inode of the edge's OWN accepted socket for this connection. The /proc lookup requires
# the server-side row with this inode to still be established, so a client that resets
# its connection cannot have its freed 4-tuple re-used by a tailscaled connection
# between the request and the lookup.
TRANSPORT_INODE_SCOPE_KEY = "viberpit.transport_inode"
_REWRITTEN_PEER_LOG = LogThrottle()


class TransportPeerMiddleware:
    """Outermost ASGI layer: record the socket peer, then apply uvicorn's
    ProxyHeadersMiddleware here, with uvicorn's own trusted-host default, so
    ``request.client`` (and every check reading it) behaves exactly as before.

    The edge unit runs ``uvicorn --no-proxy-headers`` so this is the only rewrite. If
    uvicorn still rewrites first (an older unit), the recorded peer is itself an
    ``X-Forwarded-For`` entry; ``human_gate_via`` detects that and fails closed.
    """

    def __init__(self, app) -> None:
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware  # noqa: PLC0415
        self.app = ProxyHeadersMiddleware(
            app, trusted_hosts=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"))

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket"):
            scope[TRANSPORT_PEER_SCOPE_KEY] = scope.get("client")
            scope[TRANSPORT_INODE_SCOPE_KEY] = _transport_inode(receive)
        await self.app(scope, receive, send)


def _transport_inode(receive) -> int | None:
    """Inode of the accepted socket behind *receive*, or None. uvicorn hands every ASGI
    app a ``receive`` bound to an object holding the asyncio ``transport`` (the HTTP
    request cycle, or the WebSocket protocol); anything else yields None, and the gate
    then fails closed."""
    transport = getattr(getattr(receive, "__self__", None), "transport", None)
    try:
        sock = transport.get_extra_info("socket") if transport is not None else None
        return os.fstat(sock.fileno()).st_ino if sock is not None else None
    except (OSError, ValueError, AttributeError):
        return None


def _forwarded_entry(value: str) -> tuple[str, int]:
    """One ``X-Forwarded-For`` entry as ``(host, port)``, parsed the way uvicorn's
    ProxyHeadersMiddleware does, so a peer it produced compares equal."""
    if value.startswith("["):
        end = value.find("]")
        if end != -1 and value[end + 1:].startswith(":"):
            try:
                return value[1:end], int(value[end + 2:])
            except ValueError:
                return value[1:end], 0
        return (value[1:end], 0) if end != -1 else (value, 0)
    if value.count(":") == 1:
        host, port = value.rsplit(":", 1)
        try:
            return host, int(port)
        except ValueError:
            return value, 0
    return value, 0


def _peer_looks_rewritten(conn: Request | WebSocket, host: str, port: int) -> bool:
    """True when *host*:*port* may have come from ``X-Forwarded-For`` instead of the
    socket: a real TCP peer never has port 0, and a rewritten one always equals one of
    the header's entries."""
    if not port:
        return True
    entries = ",".join(conn.headers.getlist("x-forwarded-for"))
    return any(_forwarded_entry(e.strip()) == (host, port)
               for e in entries.split(",") if e.strip())


def _proc_net_hex(addr: _AnyAddress) -> str:
    """*addr* as ``/proc/net/tcp{,6}`` prints it: each 32-bit word in host byte order."""
    packed = addr.packed
    return "".join(f"{struct.unpack('=I', packed[i:i + 4])[0]:08X}"
                   for i in range(0, len(packed), 4))


def _endpoint_keys(host: str, port: int) -> set[str]:
    """Every ``ADDR:PORT`` spelling *host* may have across the v4 and v6 tables: a v4
    client of a dual-stack listener is reported as ``::ffff:a.b.c.d`` but its socket
    sits in the v4 table, and vice versa."""
    addr: _AnyAddress = ipaddress.ip_address(host)
    forms: set[_AnyAddress] = {addr}
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        forms.add(mapped)
    elif isinstance(addr, ipaddress.IPv4Address):
        forms.add(ipaddress.IPv6Address(f"::ffff:{addr}"))
    return {f"{_proc_net_hex(a)}:{port:04X}" for a in forms}


def peer_socket_uids(peer: tuple[str, int], server: tuple[str, int] | None,
                     server_inode: int | None) -> set[int] | None:
    """Owner uids of the CLIENT end of the connection *peer* → *server*, if that socket
    lives on this host; an empty set when it does not (a remote peer).

    ``None`` (unattributable) when the tables cannot be read, an address is not an IP,
    or the edge's own end, the established row with inode *server_inode*, is not there:
    then the connection is gone and its tuple may already belong to someone else. A
    matching client row that is not a live, established socket (a timewait/orphaned
    entry, printed as uid 0) contributes ``_UNATTRIBUTABLE_UID``. More than one uid
    means the caller cannot attribute it.
    """
    if server is None or server_inode is None:
        return None
    try:
        peer_keys = _endpoint_keys(peer[0], int(peer[1]))
        server_keys = _endpoint_keys(server[0], int(server[1]))
    except (ValueError, TypeError):
        return None
    ours = False
    uids: set[int] = set()
    try:
        for path in _PROC_NET_TCP:
            try:
                with open(path, encoding="ascii") as fh:
                    next(fh, None)  # header
                    for line in fh:
                        f = line.split()
                        if len(f) < 10:
                            continue
                        if f[1] in server_keys and f[2] in peer_keys:
                            ours = ours or (f[3] == _TCP_ESTABLISHED
                                            and f[9] == str(server_inode))
                            continue
                        if f[1] not in peer_keys or f[2] not in server_keys:
                            continue
                        live = f[3] == _TCP_ESTABLISHED and f[9] != "0"
                        uids.add(int(f[7]) if live else _UNATTRIBUTABLE_UID)
            except FileNotFoundError:
                continue  # no IPv6 stack
    except (OSError, ValueError):
        return None
    return uids if ours else None


def human_gate_via(conn: Request | WebSocket) -> str | None:
    """``via`` for a human-only gate (``session_actions.require_human``): the attribution
    the middleware already recorded, else ``LOCAL_PROCESS_VIA`` unless
    ``attested_front_end_hop`` vouches that a remote person is behind this request
    (#2980). With ``rules_trust_local_host`` on, that unattested local caller is
    ``LOCAL_TRUSTED_VIA`` instead; a recorded (token-borne) ``via`` is never upgraded.
    Reads ``/proc``, so call it only on the rare writes it guards.
    """
    via = getattr(conn.state, "via", None)
    if via is not None:
        return via
    if attested_front_end_hop(conn):
        return None
    return LOCAL_TRUSTED_VIA if get_settings().rules_trust_local_host else LOCAL_PROCESS_VIA


def attested_front_end_hop(conn: Request | WebSocket) -> bool:
    """True only when this request reached the edge through a front-end on behalf of a
    client that is not this host.

    Threat note: docs/security/2026-10-11-local-identity-assertion.md. Every step fails
    closed:
    1. The peer is the socket ``TransportPeerMiddleware`` recorded, never the
       ``X-Forwarded-For``-rewritten ``scope["client"]``; a recorded peer that itself
       came from that header (uvicorn still rewriting) is refused.
    2. That socket must be live, established, and owned by exactly one front-end
       account (``_FRONT_ENDS``). Any other local owner is a process on this host,
       whatever identity header or X-Real-IP it sends.
    3. The original client the front-end stamped must be another tailnet device: a
       tailnet address this host does not own. Otherwise a local process went THROUGH
       the front-end (``curl https://<this node>``, or nginx's 0.0.0.0 listener via a
       LAN address), which stamps this node owner's login.
    A peer with no socket here at all is remote, verified by tailscale (whois or a
    VIBERPIT_TRUSTED_PROXY_IPS hop).
    """
    if TRANSPORT_PEER_SCOPE_KEY not in conn.scope:
        return False  # not behind TransportPeerMiddleware: no real peer known
    peer = conn.scope[TRANSPORT_PEER_SCOPE_KEY]
    if not peer or not peer[0]:
        return False
    host, port = str(peer[0]), int(peer[1] or 0)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True  # a non-IP test transport ("testclient") has no socket to inspect
    if _peer_looks_rewritten(conn, host, port):
        log_throttled(
            logger, _REWRITTEN_PEER_LOG, "rewritten", logging.WARNING,
            "cannot attest a human-only write: the transport peer came from X-Forwarded-For, "
            "so uvicorn is still rewriting it; run the edge with --no-proxy-headers (#2980)")
        return False
    server = conn.scope.get("server")
    uids = peer_socket_uids((host, port), tuple(server) if server else None,
                            conn.scope.get(TRANSPORT_INODE_SCOPE_KEY))
    if uids is None:
        return False
    if not uids:
        return not _is_this_host(host)
    origin = _stamped_origin(conn, uids)
    return origin is not None and _is_another_tailnet_device(origin)


def _stamped_origin(conn: Request | WebSocket, uids: set[int]) -> str | None:
    """The original client the front-end owning the socket (*uids*) stamped, or None
    when the owner is not exactly one front-end or it stamped nothing."""
    if len(uids) != 1:
        return None
    header = _front_end_headers().get(next(iter(uids)))
    if header is None:
        return None
    values = [v.strip() for v in ",".join(conn.headers.getlist(header)).split(",")
              if v.strip()]
    # tailscaled SETS X-Forwarded-For to the source address; were it ever to append
    # instead, the last entry is still the one it wrote. nginx overwrites X-Real-IP.
    return _forwarded_entry(values[-1])[0] if values else None


def _rewritten_by_x_forwarded_for(conn: Request | WebSocket) -> bool:
    """True when ``scope["client"]`` is not the recorded socket peer, i.e.
    TransportPeerMiddleware rewrote it from ``X-Forwarded-For``."""
    peer = conn.scope.get(TRANSPORT_PEER_SCOPE_KEY)
    return peer is not None and tuple(peer) != tuple(conn.scope.get("client") or ())


def _front_end_origin(conn: Request | WebSocket) -> str | None:
    """The client a front-end relayed this connection for, read from the header THAT
    front-end stamps (``_FRONT_ENDS``), or None: a socket no single front-end owns, or
    one that cannot be attributed, names no one."""
    peer = conn.scope[TRANSPORT_PEER_SCOPE_KEY]
    server = conn.scope.get("server")
    try:
        uids = peer_socket_uids((str(peer[0]), int(peer[1] or 0)),
                                tuple(server) if server else None,
                                conn.scope.get(TRANSPORT_INODE_SCOPE_KEY))
    except (TypeError, ValueError, IndexError):
        return None
    return _stamped_origin(conn, uids) if uids else None


# Tailscale's address ranges: CGNAT for IPv4, its ULA prefix for IPv6.
_TAILNET_NETS = (ipaddress.ip_network("100.64.0.0/10"),
                 ipaddress.ip_network("fd7a:115c:a1e0::/48"))


def _is_this_host(host: str) -> bool:
    """True when *host* is loopback, any address this host owns (bindable here: LAN,
    docker, tailnet interface) or one of this node's tailnet addresses, or when it
    cannot be judged (not an IP, or this node's tailnet addresses are unknown)."""
    try:
        addr: _AnyAddress = ipaddress.ip_address(host)
    except ValueError:
        return True
    addr = getattr(addr, "ipv4_mapped", None) or addr
    if addr.is_loopback:
        return True
    own = _self_tailscale_ips()
    if not own or addr in own:
        return True
    family = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as probe:
            probe.bind((str(addr), 0))
        return True  # assigned to an interface here
    except OSError:
        return False


def _is_another_tailnet_device(host: str) -> bool:
    """The origin a front-end stamped is a person elsewhere only if it is a tailnet
    address (``tailscale whois`` can name it) that this host does not own. A LAN or
    docker address of this host, reached through nginx's 0.0.0.0 listener, is local."""
    try:
        addr: _AnyAddress = ipaddress.ip_address(host)
    except ValueError:
        return False
    addr = getattr(addr, "ipv4_mapped", None) or addr
    return any(addr in net for net in _TAILNET_NETS) and not _is_this_host(str(addr))


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
        rewritten = _rewritten_by_x_forwarded_for(request)
        if rewritten:
            # request.client is X-Forwarded-For, and only tailscaled sets that header.
            # nginx overwrites X-Real-IP; an X-Forwarded-For it passes through is the
            # client's own choice, so whois must not follow it (#3010).
            peer_ip = forwarded = _front_end_origin(request)
        # Only honour X-Real-IP when something entitled to set it sits in front:
        # loopback, one of this node's own tailnet addresses, or an address in
        # VIBERPIT_TRUSTED_PROXY_IPS. This is the same predicate the identity-header
        # gate uses — one peer-trust rule in this module, not two that can drift
        # apart. Evaluated only when there IS a forwarded address for it to gate:
        # with no X-Real-IP the verdict cannot change peer_ip, and since #2134 the
        # check can cost a `tailscale status` spawn — the exact per-request cost
        # this whole path exists to stop paying.
        if forwarded and not rewritten and _peer_is_trusted(request):
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
