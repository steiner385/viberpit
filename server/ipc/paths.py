"""Well-known endpoint for the session daemon — single source of truth for edge + daemon.

Supports two transports:
  * unix:///path   — same-host Unix socket (default; trust = file permissions).
  * https://host:port — cross-host TCP with mutual TLS (see server/ipc/tls.py).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

from server.hostpaths import SESSIOND_SOCKET as DEFAULT_SOCKET_PATH


@dataclass(frozen=True)
class Endpoint:
    scheme: str                 # "unix" | "https"
    path: str | None = None     # unix only
    host: str | None = None     # https only
    port: int | None = None     # https only

    @property
    def is_unix(self) -> bool:
        return self.scheme == "unix"

    @property
    def is_tcp(self) -> bool:
        return self.scheme == "https"

    def __str__(self) -> str:
        """The endpoint as a URL — the exact inverse of parse_endpoint.

        Without this, str() falls through to the dataclass repr
        ("Endpoint(scheme='https', ...)"), which parse_endpoint rejects. That is
        not hypothetical: ensure_default_host stored str(endpoint) as the fleet
        host's endpoint, so the registry's only host was permanently unhealthy
        with "ValueError: unsupported daemon endpoint" (production, 2026-08-22).

        Defined here rather than fixed at the call site so the mistake cannot
        recur anywhere else that stringifies an Endpoint.
        """
        if self.is_unix:
            return f"unix://{self.path}"
        return f"https://{self.host}:{self.port}"


def parse_endpoint(raw: str) -> Endpoint:
    """Parse a daemon endpoint string. A bare path is treated as a unix socket."""
    raw = raw.strip()
    if raw.startswith("unix://"):
        path = raw[len("unix://"):]
        if not path:
            raise ValueError(f"unix endpoint requires a path: {raw!r}")
        return Endpoint("unix", path=path)
    if raw.startswith("https://"):
        u = urlparse(raw)
        if not u.hostname or not u.port:
            raise ValueError(f"https endpoint requires host and port: {raw!r}")
        return Endpoint("https", host=u.hostname, port=u.port)
    if raw.startswith("/"):
        return Endpoint("unix", path=raw)
    raise ValueError(f"unsupported daemon endpoint: {raw!r}")


#: Hosts whose daemon is this process's own, reached over TCP rather than the socket.
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def endpoint_host_name(ep: Endpoint) -> str:
    """The ``hosts.name`` the daemon at *ep* answers to.

    Callers that hold only an endpoint (``DaemonClient``) still need the fleet NAME to
    ask ``host_upgrade`` whether that host is being restarted on purpose. Deriving it
    here keeps one rule instead of a name threaded through every construction site.

    A unix socket or a loopback address is this host by definition, so it resolves
    through ``self_host_name`` — which is also what the deploy controller uses for its
    own ``local_name``, so the two agree on what to call dobby. Anything else is a
    worker, named for the first label of its endpoint: that is exactly how
    auto-registered hosts are named (the first label of the tailnet MagicDNS name —
    see ``server.hostname.self_host_name``).

    A host renamed via ``VIBERPIT_HOST_NAME`` so that it no longer matches its endpoint
    simply fails to match a deploy and reports its restarts at WARNING — the same
    degradation ``self_host_name``'s own docstring warns about, and the safe direction.
    """
    from server.hostname import self_host_name

    if ep.is_unix or (ep.host or "") in _LOOPBACK:
        return self_host_name()
    return (ep.host or "").split(".")[0]


def sessiond_endpoint() -> Endpoint:
    """Resolve the daemon endpoint: VIBERPIT_SESSIOND_ENDPOINT, else the legacy
    VIBERPIT_SESSIOND_SOCKET (bare path → unix://), else the well-known default."""
    raw = os.environ.get("VIBERPIT_SESSIOND_ENDPOINT")
    if raw:
        return parse_endpoint(raw)
    sock = os.environ.get("VIBERPIT_SESSIOND_SOCKET")
    if sock:
        return parse_endpoint(sock if "://" in sock else f"unix://{sock}")
    return Endpoint("unix", path=DEFAULT_SOCKET_PATH)
