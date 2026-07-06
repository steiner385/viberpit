"""GitHub Copilot provider: the single source of truth for the copilot account type.

Copilot accounts are activated not by CLAUDE_CONFIG_DIR OAuth but by pointing claude at a
localhost translating proxy. This module owns: the provider constant, the proxy /usage ->
Usage mapping (so the provider-agnostic selector ranks copilot accounts unchanged), and the
launch-env builder.
"""
from __future__ import annotations

import logging
from datetime import datetime

from .oauth import Usage

log = logging.getLogger(__name__)

PROVIDER = "copilot"


def usage_from_proxy(payload: dict, now: datetime) -> Usage | None:
    """Map the proxy /usage premium-request quota into a Usage.

    Copilot Pro+ meters a single ~monthly premium-request allowance, so we project it onto
    BOTH the session and weekly dimensions (the selector takes whichever binds first; here
    they are identical). Returns None for a missing/garbage payload so callers degrade to
    'usage unknown' rather than a false ceiling.
    """
    block = payload.get("premium") if isinstance(payload.get("premium"), dict) else payload
    try:
        used = float(block["used"])
        limit = float(block["limit"])
    except (KeyError, TypeError, ValueError):
        return None
    if limit <= 0:
        return None
    pct = max(0.0, min(100.0, 100.0 * used / limit))
    resets_at = _parse_iso(block.get("resets_at"))
    return Usage(
        session_pct=pct, session_resets_at=resets_at,
        weekly_pct=pct, weekly_resets_at=resets_at,
        extra_usage_enabled=False,
    )


def _parse_iso(s) -> datetime | None:
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None
