"""GitHub Copilot provider: the single source of truth for the copilot account type.

Copilot accounts are activated not by CLAUDE_CONFIG_DIR OAuth but by pointing claude at a
localhost translating proxy. This module owns: the provider constant, the proxy /usage ->
Usage mapping (so the provider-agnostic selector ranks copilot accounts unchanged), and the
launch-env builder.
"""
from __future__ import annotations

import logging
from datetime import datetime

from . import paths
from .quota import QuotaSnapshot, copilot_snapshot

log = logging.getLogger(__name__)

PROVIDER = "copilot"


def usage_from_proxy(payload: dict, now: datetime) -> QuotaSnapshot | None:
    """Map the proxy /usage premium-request quota into a QuotaSnapshot.

    Copilot Pro+ meters exactly ONE allowance (premium requests), so the snapshot
    has exactly one window. The predecessor projected that single number onto both
    a "session" and a "weekly" dimension because the old ``Usage`` dataclass had
    nowhere else to put it; the duplicate then surfaced as two identical badges in
    the accounts UI and made "which dimension binds?" a meaningless question for
    this provider. Fabricating it is now impossible — the window list holds what
    the provider reported and nothing else.

    Returns None for a missing/garbage payload so callers degrade to 'usage
    unknown' rather than a false ceiling.
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
    return copilot_snapshot(pct, resets_at, fetched_at=now)


def _parse_iso(s) -> datetime | None:
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def copilot_account_names() -> list[str]:
    d = paths.copilot_store_dir()
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir()
                  if p.is_dir() and (p / "credentials.json").exists())


def launch_env(port: int, secret: str) -> dict[str, str]:
    """Env that makes claude route through the per-account proxy instead of OAuth.

    ENABLE_TOOL_SEARCH is set because claude disables MCP tool search by default for a
    non-first-party ANTHROPIC_BASE_URL.
    """
    return {
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
        "ANTHROPIC_AUTH_TOKEN": secret,
        "ENABLE_TOOL_SEARCH": "true",
    }
