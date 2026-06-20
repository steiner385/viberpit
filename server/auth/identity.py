"""Tailscale identity resolution (T028).

Resolves the caller's verified identity from Tailscale-provided request headers.
Primary source: Tailscale-User-Login (set by `tailscale serve` proxy).
Fallback: X-Tailscale-User-Login (alternate header name some setups use).

Allowlist: if Settings.tailnet_allowlist is non-empty, identity must be in the
list or None is returned (deny). If empty (dev default), any identity is accepted.
"""
from __future__ import annotations

import subprocess
from starlette.requests import Request

from server.config import get_settings


def verify_identity(request: Request) -> str | None:
    """Return the verified identity string, or None if unverifiable/denied."""
    identity = (
        request.headers.get("Tailscale-User-Login")
        or request.headers.get("X-Tailscale-User-Login")
    )

    if not identity:
        # Best-effort: try `tailscale whois` on the peer address
        identity = _whois_identity(request)

    if not identity:
        return None

    # Allowlist check
    settings = get_settings()
    allowlist = settings.tailnet_allowlist_items
    if allowlist and identity not in allowlist:
        return None

    return identity


def _whois_identity(request: Request) -> str | None:
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
