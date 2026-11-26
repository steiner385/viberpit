"""Which listed sessions ViberPit shows as remote sessions, and the summary it shows.

A listed session is shown when it is a Remote Control session (``environment_kind ==
"bridge"``; ``anthropic_cloud`` ones are claude.ai's own cloud sessions), not archived,
and either connected or disconnected within ``DISCONNECTED_GRACE_S`` (shown as
disconnected, so a CLI that just dropped does not blink out of the list). A session
ViberPit itself runs with Remote Control on is excluded: its ``id`` or ``session_url``
matches a ViberPit record's ``remote_control_url``.

Pure functions over the raw API objects (verified keys, plan "Verified"): any field of an
unexpected type reads as absent rather than failing the listing.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from server.remote_sessions.client import valid_session_id

ENV_BRIDGE = "bridge"
STATUS_ARCHIVED = "archived"
STATUS_REQUIRES_ACTION = "requires_action"
CONNECTED = "connected"
DISCONNECTED = "disconnected"
#: How long a disconnected Remote Control session stays listed after its last update.
DISCONNECTED_GRACE_S = 15 * 60
MACHINE_TAG_PREFIX = "machine-id:"
#: Remote Control origin tags -> the short origin ViberPit reports.
ORIGIN_TAGS = {"remote-control-repl": "repl", "remote-control-sdk": "sdk",
               "remote-control-auto": "auto"}


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _epoch(value: Any) -> float | None:
    """An API timestamp (ISO 8601, ``Z`` suffix) as epoch seconds, or None."""
    text = _str(value)
    if text is None:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def session_key(url_or_id: str) -> str:
    """The ``session_…`` id a claude.ai session URL (or a bare id) names."""
    return url_or_id.rstrip("/").rsplit("/", 1)[-1]


def owned_keys(urls: Iterable[str | None]) -> frozenset[str]:
    """The session ids of ViberPit's own Remote Control sessions, from their records'
    ``remote_control_url``."""
    return frozenset(session_key(u) for u in urls if isinstance(u, str) and u)


def _projects(ext: dict) -> list[dict]:
    """``[{repo, branch}]`` from ``external_metadata.current_branches`` (``{"owner/repo":
    "branch"}``), in the API's order. A bridge session's ``session_context.cwd`` is empty,
    so this is the only place its project shows. Non-string keys are skipped; a
    non-string branch reads as None."""
    branches = ext.get("current_branches")
    if not isinstance(branches, dict):
        return []
    return [{"repo": repo, "branch": _str(branch)}
            for repo, branch in branches.items() if _str(repo) is not None]


def summarize(raw: Any, account: str, *, now: float,
              owned: frozenset[str] = frozenset()) -> dict | None:
    """The summary ViberPit shows for one listed session, or None when it is not shown
    (see the module docstring). *now* is epoch seconds."""
    if not isinstance(raw, dict):
        return None
    sid = _str(raw.get("id"))
    if not valid_session_id(sid) or raw.get("environment_kind") != ENV_BRIDGE:
        return None
    status = _str(raw.get("session_status"))
    if status is None or status == STATUS_ARCHIVED:
        return None
    url = _str(raw.get("session_url"))
    if sid in owned or (url is not None and session_key(url) in owned):
        return None
    connection = _str(raw.get("connection_status"))
    updated = _epoch(raw.get("updated_at"))
    if connection != CONNECTED:
        if updated is None or now - updated > DISCONNECTED_GRACE_S:
            return None
        connection = DISCONNECTED
    tags = [t for t in raw.get("tags") or [] if isinstance(t, str)] \
        if isinstance(raw.get("tags"), list) else []
    machine = next((t[len(MACHINE_TAG_PREFIX):] for t in tags
                    if t.startswith(MACHINE_TAG_PREFIX)), None)
    origin = next((ORIGIN_TAGS[t] for t in tags if t in ORIGIN_TAGS), None)
    ctx_raw = raw.get("session_context")
    ctx: dict = ctx_raw if isinstance(ctx_raw, dict) else {}
    ext_raw = raw.get("external_metadata")
    ext: dict = ext_raw if isinstance(ext_raw, dict) else {}
    projects = _projects(ext)
    return {
        "id": sid,
        "account": account,
        "title": _str(raw.get("title")) or "",
        "status": status,
        "connection": connection,
        "machineId": machine or None,
        "origin": origin,
        # The first repo claude.ai reports, its branch, and every one when several.
        "project": projects[0]["repo"] if projects else None,
        "branch": projects[0]["branch"] if projects else None,
        "projects": projects if len(projects) > 1 else None,
        # Empty for a bridge session (claude.ai sends ""), so usually None.
        "cwd": _str(ctx.get("cwd")),
        "model": _str(ctx.get("model")) or _str(ext.get("model")),
        "url": url,
        "updatedAt": _str(raw.get("updated_at")),
        "needsYou": status == STATUS_REQUIRES_ACTION,
        "unread": raw.get("unread") is True,
    }


def updated_epoch(summary: dict) -> float | None:
    """A summary's ``updatedAt`` as epoch seconds, or None."""
    return _epoch(summary.get("updatedAt"))


def sort_key(summary: dict) -> tuple:
    """Needs-you first, then connected, then most recently updated."""
    return (not summary["needsYou"], summary["connection"] != CONNECTED,
            -(updated_epoch(summary) or 0.0))
