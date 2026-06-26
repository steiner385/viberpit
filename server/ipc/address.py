"""Edge↔daemon transport address: parse one env var into a UDS or TCP descriptor.

Single source of truth for both the daemon listener (server/ipc/serve.py) and the
edge client (server/ipc/client.py) so neither re-implements transport branching.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
import websockets

from server.ipc.paths import sessiond_socket

log = logging.getLogger("server.ipc.address")

# Hosts that would expose the no-app-auth daemon beyond the intended tailnet bind.
_WILDCARD_HOSTS = {"", "0.0.0.0", "::", "*"}


def _bracket(host: str) -> str:
    return f"[{host}]" if ":" in host else host


@dataclass(frozen=True)
class DaemonAddress:
    kind: str                       # "unix" | "tcp"
    uds_path: str | None = None
    host: str | None = None
    port: int | None = None

    def uvicorn_kwargs(self) -> dict:
        if self.kind == "unix":
            return {"uds": self.uds_path}
        return {"host": self.host, "port": self.port}

    def httpx_base_url(self) -> str:
        if self.kind == "unix":
            return "http://daemon"
        assert self.host is not None  # tcp branch always has host (parser guarantees it)
        return f"http://{_bracket(self.host)}:{self.port}"

    def httpx_client_kwargs(self) -> dict:
        if self.kind == "unix":
            return {"transport": httpx.AsyncHTTPTransport(uds=self.uds_path),
                    "base_url": "http://daemon"}
        return {"base_url": self.httpx_base_url()}

    def ws_uri(self) -> str:
        if self.kind == "unix":
            return "ws://daemon/events"
        assert self.host is not None  # tcp branch always has host (parser guarantees it)
        return f"ws://{_bracket(self.host)}:{self.port}/events"

    def ws_connect(self):
        """Return the websockets connect context-manager (use with `async with`)."""
        if self.kind == "unix":
            return websockets.unix_connect(self.uds_path, self.ws_uri())
        return websockets.connect(self.ws_uri())


def parse_daemon_url(url: str) -> DaemonAddress:
    parts = urlsplit(url)
    if parts.scheme == "unix":
        if not parts.path:
            raise ValueError(f"unix daemon URL needs a socket path: {url!r}")
        return DaemonAddress(kind="unix", uds_path=parts.path)
    if parts.scheme == "tcp":
        host = parts.hostname               # strips [] for IPv6, lowercases
        try:
            port = parts.port               # raises ValueError on out-of-range / non-numeric
        except ValueError as exc:
            raise ValueError(f"invalid daemon TCP port in {url!r}") from exc
        if host is None or host in _WILDCARD_HOSTS:
            raise ValueError(
                f"daemon TCP host must be a specific (tailnet) IP, not a wildcard: {url!r}")
        if not port:
            raise ValueError(f"daemon TCP URL needs a non-zero port: {url!r}")
        return DaemonAddress(kind="tcp", host=host, port=port)
    raise ValueError(
        f"unsupported daemon URL scheme {parts.scheme!r} (expected unix:// or tcp://): {url!r}")


def daemon_address() -> DaemonAddress:
    """Resolve the daemon transport from the environment.

    CCSWITCH_SESSIOND_URL wins when set (logs a warning if CCSWITCH_SESSIOND_SOCKET is
    also set). Otherwise UDS at the back-compat path (sessiond_socket())."""
    url = os.environ.get("CCSWITCH_SESSIOND_URL")
    if url:
        if os.environ.get("CCSWITCH_SESSIOND_SOCKET"):
            log.warning("both CCSWITCH_SESSIOND_URL and CCSWITCH_SESSIOND_SOCKET set — using URL")
        return parse_daemon_url(url)
    return DaemonAddress(kind="unix", uds_path=sessiond_socket())
