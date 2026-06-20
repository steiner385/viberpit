"""WebAuthn/passkey scaffold (T029 — disabled flag).

Passkey registration and authentication are deferred to v1.1.0 (Phase 4).
These functions raise NotImplementedError until WEBAUTHN_ENABLED=True.

Do NOT wire these endpoints into the router until the flag is enabled.
"""
from __future__ import annotations

import os

from fastapi import HTTPException


WEBAUTHN_ENABLED: bool = os.environ.get("WEBAUTHN_ENABLED", "false").lower() == "true"


def _require_enabled() -> None:
    if not WEBAUTHN_ENABLED:
        raise HTTPException(status_code=404, detail="WebAuthn support is not enabled in v1")


async def begin_registration(identity: str) -> dict:
    """Begin passkey registration for the given identity."""
    _require_enabled()
    raise NotImplementedError("WebAuthn registration not yet implemented")


async def complete_registration(identity: str, credential: dict) -> dict:
    """Complete passkey registration."""
    _require_enabled()
    raise NotImplementedError("WebAuthn registration not yet implemented")


async def begin_authentication(identity: str | None = None) -> dict:
    """Begin passkey authentication challenge."""
    _require_enabled()
    raise NotImplementedError("WebAuthn authentication not yet implemented")


async def complete_authentication(credential: dict) -> str:
    """Verify passkey assertion; return verified identity on success."""
    _require_enabled()
    raise NotImplementedError("WebAuthn authentication not yet implemented")
