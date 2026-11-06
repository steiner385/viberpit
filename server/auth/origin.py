"""THE Origin allowlist: cross-site WebSocket hijacking and cross-origin writes.

Identity is the Tailscale header the front-end stamps on every request, so the
browser's DEVICE is what authenticates, not the page. Without this check any page open
in a tailnet browser (any origin) could open ``/ws`` and send prompts as the owner, or
POST to a body-less mutating route. One ASGI layer, :class:`OriginGuardMiddleware`,
applies the one rule (:func:`origin_allowed`) to:

* every WebSocket upgrade, whatever route it targets (closed with 1008 before accept);
* every state-changing HTTP method (POST/PUT/PATCH/DELETE), answered 403.

Allowed origins:

* exactly ``VIBERPIT_PUBLIC_ORIGIN``, each ``VIBERPIT_ALLOWED_ORIGINS`` entry, and in
  ``VIBERPIT_DEV=1`` the Vite dev/preview origins;
* same-origin: the Origin's ``host[:port]`` equals the ``Host`` (or the front-end's
  ``X-Forwarded-Host``) the request arrived with, AND its hostname is one this edge
  answers to (a configured origin's hostname, or loopback). That covers
  ``tailscale serve`` on another port of the public hostname, and a direct loopback
  hit, without trusting an attacker's own hostname after a DNS rebind.

A request with NO Origin is a non-browser client (the CLI, ``viberpit ask``, the
assistant's loopback client, webhooks): browsers always send Origin on a WebSocket
upgrade and on a cross-origin write. As a backstop for a browser that omits it,
``Sec-Fetch-Site: cross-site|same-site`` (only browsers send it) is refused too.
A present Origin that is not allowed — including ``null`` — fails closed.
"""
from __future__ import annotations

import logging
import os
import urllib.parse
from collections.abc import Iterable

from server.config import get_settings

log = logging.getLogger(__name__)

#: Methods that change state. GET/HEAD/OPTIONS stay unchecked (read-only by contract).
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Vite ``dev`` (5173) and ``preview`` (4173) origins, admitted only under VIBERPIT_DEV=1.
DEV_ORIGINS = tuple(f"http://{h}:{p}" for h in ("localhost", "127.0.0.1") for p in (5173, 4173))

_LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "[::1]"})

#: WebSocket close code for a refused Origin: 1008 Policy Violation.
WS_CLOSE_POLICY_VIOLATION = 1008
REFUSAL_REASON = "origin not allowed"
REFUSAL_CODE = "ORIGIN_FORBIDDEN"


def normalize_origin(raw: str | None) -> str | None:
    """``scheme://host[:port]`` lower-cased with a default port dropped, or None for
    anything that is not an http(s)/ws(s) origin (``null``, a path, userinfo …)."""
    value = (raw or "").strip()
    if not value or any(c.isspace() for c in value):
        return None
    parts = urllib.parse.urlsplit(value)
    scheme = parts.scheme.lower()
    if (scheme not in ("http", "https") or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.path not in ("", "/")
            or parts.query or parts.fragment):
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default = 443 if scheme == "https" else 80
    return f"{scheme}://{host}" + (f":{port}" if port not in (None, default) else "")


def _netloc(origin: str) -> str:
    return origin.split("://", 1)[1]


def _hostname(netloc: str) -> str:
    if netloc.startswith("["):
        return netloc[: netloc.index("]") + 1]
    return netloc.split(":", 1)[0]


def configured_origins() -> frozenset[str]:
    """The explicit allowlist, read from settings on each call (tests patch env)."""
    settings = get_settings()
    raw: list[str] = []
    if settings.public_origin:
        raw.append(settings.public_origin)
    raw.extend(item.strip() for item in (settings.allowed_origins or "").split(","))
    if os.environ.get("VIBERPIT_DEV") == "1":
        raw.extend(DEV_ORIGINS)
    return frozenset(o for o in (normalize_origin(r) for r in raw) if o is not None)


def _host_values(headers: Iterable[tuple[bytes, bytes]]) -> set[str]:
    out: set[str] = set()
    for key, value in headers:
        if key in (b"host", b"x-forwarded-host"):
            out.update(v.strip().lower() for v in value.decode("latin-1").split(",") if v.strip())
    return out


def origin_allowed(origin: str, request_hosts: set[str],
                   allowed: frozenset[str] | None = None) -> bool:
    """THE rule for a PRESENT Origin header (see the module docstring)."""
    norm = normalize_origin(origin)
    if norm is None:
        return False
    allowed = configured_origins() if allowed is None else allowed
    if norm in allowed:
        return True
    netloc = _netloc(norm)
    trusted = _LOOPBACK_HOSTNAMES | {_hostname(_netloc(o)) for o in allowed}
    if _hostname(netloc) not in trusted:
        return False
    scheme = norm.split("://", 1)[0]
    default = "443" if scheme == "https" else "80"
    return any(h == netloc or h == f"{netloc}:{default}" for h in request_hosts)


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    for key, value in headers:
        if key == name:
            return value.decode("latin-1")
    return None


_warned_mismatch: set[str] = set()


def _warn_public_origin_port_mismatch(origin: str, request_hosts: set[str]) -> None:
    """Warn once when ``VIBERPIT_PUBLIC_ORIGIN`` is trusted outright but no request host
    carries its port: the edge is probably served on another port, so whatever owns the
    configured port is trusted with writes (#3325). Never changes the verdict."""
    public = normalize_origin(get_settings().public_origin)
    if public is None or normalize_origin(origin) != public or public in _warned_mismatch:
        return
    netloc = _netloc(public)
    default = "443" if public.startswith("https://") else "80"
    seen = {h for h in request_hosts if h.rsplit(":", 1)[0] not in _LOOPBACK_HOSTNAMES}
    if seen and not any(h == netloc or h == f"{netloc}:{default}" for h in seen):
        _warned_mismatch.add(public)
        log.warning("origin.public_origin_port_mismatch configured=%s request_hosts=%s: "
                    "set VIBERPIT_PUBLIC_ORIGIN to the host:port the edge is served on",
                    public, sorted(seen))


def scope_refusal(scope) -> str | None:
    """Why this ASGI scope must be refused, or None to let it through."""
    if scope["type"] == "http" and scope.get("method", "GET").upper() not in UNSAFE_METHODS:
        return None
    headers = scope.get("headers") or []
    origin = _header(headers, b"origin")
    if origin is None:
        site = (_header(headers, b"sec-fetch-site") or "").strip().lower()
        if site in ("cross-site", "same-site"):
            return f"no Origin but Sec-Fetch-Site={site}"
        return None
    hosts = _host_values(headers)
    if origin_allowed(origin, hosts):
        _warn_public_origin_port_mismatch(origin, hosts)
        return None
    return f"Origin {origin!r} is not allowed"


class OriginGuardMiddleware:
    """Pure ASGI, so it sees WebSocket upgrades (``BaseHTTPMiddleware`` never does)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        why = scope_refusal(scope)
        if why is None:
            await self.app(scope, receive, send)
            return
        # An expected refusal (a hostile page, a misconfigured front-end): INFO, not an
        # auto-filed error.
        log.info("origin.refused type=%s path=%s %s", scope["type"], scope.get("path"), why)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": WS_CLOSE_POLICY_VIOLATION,
                        "reason": REFUSAL_REASON})
            return
        from starlette.responses import JSONResponse  # noqa: PLC0415
        response = JSONResponse(status_code=403, content={"error": {
            "code": REFUSAL_CODE,
            "message": "Cross-origin request refused. Set VIBERPIT_ALLOWED_ORIGINS to "
                       "admit another origin."}})
        await response(scope, receive, send)
