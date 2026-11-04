"""One push when a shared MCP sign-in dies (MCP auth overhaul phase 3).

:func:`send_alerts` delivers the refresher tick's alert transitions as ``mcp_auth_needed``
events, then records each one as alerted for its (key, generation), so a grant seen dead
on many ticks pushes once. A push that fails to build or send is logged and left
unmarked: the next tick, which sees the same transition again, retries it.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from server.mcp_health import grant_health
from server.sessions import push as push_mod

log = logging.getLogger(__name__)

REASON_MAX_CHARS = 160
_OUTCOME = {"alert_dead": "dead", "alert_expired": "expired"}


def alert_copy(t: grant_health.Transition) -> tuple[str, str]:
    """The push title and body for an alert transition."""
    title = f"MCP sign-in needed: {t.server}"
    if t.kind == "alert_dead":
        return title, (f"{t.server}'s sign-in was revoked or expired. Sign in once to "
                       f"restore it for every account and runner.")
    reason = (t.reason or "unknown error")[:REASON_MAX_CHARS]
    return title, (f"{t.server}'s sign-in expired — background refresh is failing: "
                   f"{reason}. Sign in again.")


async def send_alerts(transitions: Iterable[grant_health.Transition], *, orch: Any) -> None:
    """Push each alert transition once, then mark it alerted. Never raises (except
    cancellation)."""
    for t in transitions:
        outcome = _OUTCOME.get(t.kind)
        if outcome is None:
            continue
        try:
            title, body = alert_copy(t)
            await push_mod.notify_event("mcp_auth_needed", title, body, url="/settings/mcp",
                                        fanout_when_unconfigured=True, orch=orch)
        except Exception:
            log.warning("mcp sign-in alert failed server=%s; retrying next tick", t.server,
                        exc_info=True)
            continue
        try:
            await grant_health.mark_alerted(t.key, f"{outcome}:{t.generation}")
        except Exception:
            # The push went out; an unrecorded mark means one repeat push next tick.
            log.warning("mcp sign-in alert: recording delivery failed server=%s", t.server,
                        exc_info=True)
