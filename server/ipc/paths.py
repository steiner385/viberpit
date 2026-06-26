"""Well-known endpoint for the session daemon — single source of truth for edge + daemon.

Supports two transports:
  * unix:///path   — same-host Unix socket (default; trust = file permissions).
  * https://host:port — cross-host TCP with mutual TLS (see server/ipc/tls.py).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

DEFAULT_SOCKET_PATH = "/run/ccswitch/sessiond.sock"


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


def sessiond_endpoint() -> Endpoint:
    """Resolve the daemon endpoint: CCSWITCH_SESSIOND_ENDPOINT, else the legacy
    CCSWITCH_SESSIOND_SOCKET (bare path → unix://), else the well-known default."""
    raw = os.environ.get("CCSWITCH_SESSIOND_ENDPOINT")
    if raw:
        return parse_endpoint(raw)
    sock = os.environ.get("CCSWITCH_SESSIOND_SOCKET")
    if sock:
        return parse_endpoint(sock if "://" in sock else f"unix://{sock}")
    return Endpoint("unix", path=DEFAULT_SOCKET_PATH)


def sessiond_socket() -> str:
    """The daemon UDS path: CCSWITCH_SESSIOND_SOCKET if set, else the well-known default.

    Unchanged legacy accessor (same-host callers, e.g. the deploy controller)."""
    return os.environ.get("CCSWITCH_SESSIOND_SOCKET", DEFAULT_SOCKET_PATH)
