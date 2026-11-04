"""One push when a shared MCP sign-in dies (MCP auth overhaul phase 3).

:func:`send_alerts` delivers :func:`grant_health.pending_alerts` as ``mcp_auth_needed``
events, then records each one as alerted for its (state, generation), so a grant seen dead
on many ticks pushes once. ``notify_event`` never raises; if building the copy or recording
the delivery fails, the alert is logged and stays pending, so the next tick (which
re-derives pending alerts from the table) retries it.
"""
from __future__ import annotations

import logging
import urllib.parse
from collections.abc import Iterable
from typing import Any

from server.config import get_settings
from server.mcp_health import grant_health
from server.sessions import push as push_mod

log = logging.getLogger(__name__)

REASON_MAX_CHARS = 160
_STATE = {"alert_dead": "dead", "alert_expired": "expired"}


def alert_copy(t: grant_health.Transition) -> tuple[str, str]:
    """The push title and body for an alert transition."""
    title = f"MCP sign-in needed: {t.server}"
    if t.kind == "alert_dead":
        return title, (f"{t.server}'s sign-in was revoked or expired. Sign in once to "
                       f"restore it for every account and runner.")
    reason = (t.reason or "unknown error")[:REASON_MAX_CHARS]
    return title, (f"{t.server}'s sign-in expired — background refresh is failing: "
                   f"{reason}. Sign in again.")


def _alert_url(server: str) -> str:
    """One-tap sign-in link when the public origin is set (the start route falls back to
    the paste-back page itself), else the Settings page."""
    if get_settings().public_origin:
        return f"/api/mcp/oauth/start?server={urllib.parse.quote(server, safe='')}"
    return "/settings/mcp"


async def send_alerts(transitions: Iterable[grant_health.Transition], *, orch: Any) -> None:
    """Push each alert transition once, then mark it alerted. Never raises (except
    cancellation)."""
    for t in transitions:
        state = _STATE.get(t.kind)
        if state is None:
            continue
        try:
            title, body = alert_copy(t)
        except Exception:
            log.warning("mcp sign-in alert failed server=%s; retrying next tick", t.server,
                        exc_info=True)
            continue
        await push_mod.notify_event("mcp_auth_needed", title, body, url=_alert_url(t.server),
                                    fanout_when_unconfigured=True, orch=orch)
        try:
            await grant_health.mark_alerted(t.key, f"{state}:{t.generation}")
        except Exception:
            # The push went out; an unrecorded mark means one repeat push next tick.
            log.warning("mcp sign-in alert: recording delivery failed server=%s", t.server,
                        exc_info=True)
