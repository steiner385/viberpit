"""Remote Control: a ViberPit Claude session also appears in the Claude web / desktop /
mobile apps (Claude Code "Remote Control").

Mechanism (verified against the real CLI, plan 2026-11-24): the CLI's SDK control
channel accepts ``{"subtype": "remote_control", "enabled": true}`` and replies with
``{"session_url", "connect_url", "environment_id"}``; ``enabled: false`` tears the
bridge down and replies ``{}``. The settings.json ``remoteControlAtStartup`` key is read
only by the interactive REPL, so every SDK spawn/resume must send the request itself.

Semantics, in one place so the driver, the orchestrator, the session payload and the
settings route cannot drift:

* **Global default** — ``AppSettings.remote_control_default`` (NULL = on).
* **Per-session override** — ``Session.remote_control`` (NULL = follow the default).
* **Kill switch** — ``VIBERPIT_REMOTE_CONTROL_ENABLED`` (default on). Off: nothing is
  ever sent, every session is unavailable and the settings capability reads false.
* **Eligibility** — the Claude engine, not a Copilot account (no claude.ai OAuth), not
  the Assistant drawer session.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

log = logging.getLogger(__name__)

#: The CLI registers an environment with claude.ai before it answers; the spike saw a
#: few seconds, so this is generous.
REMOTE_CONTROL_TIMEOUT_S = 45.0

#: The session cannot use Remote Control: kill switch off, wrong engine, a Copilot
#: account or the Assistant drawer.
REMOTE_CONTROL_UNAVAILABLE = "REMOTE_CONTROL_UNAVAILABLE"
#: The session has no live CLI right now. The orchestrator persists the choice and the
#: next spawn applies it; never surfaced to a caller.
REMOTE_CONTROL_NOT_LIVE = "REMOTE_CONTROL_NOT_LIVE"
#: The CLI answered the request with an error.
REMOTE_CONTROL_REFUSED = "REMOTE_CONTROL_REFUSED"
#: The request got no reply (timeout, transport): it may or may not have landed.
REMOTE_CONTROL_FAILED = "REMOTE_CONTROL_FAILED"
REMOTE_CONTROL_CODES = frozenset({REMOTE_CONTROL_UNAVAILABLE, REMOTE_CONTROL_NOT_LIVE,
                                  REMOTE_CONTROL_REFUSED, REMOTE_CONTROL_FAILED})

#: How long the edge's cached copy of the app default may be served before a request
#: re-reads it (the session payload is built synchronously, so it cannot read the DB).
DEFAULT_CACHE_TTL_S = 30.0


class RemoteControlError(Exception):
    """An expected Remote Control refusal; ``code`` is one of ``REMOTE_CONTROL_CODES``."""

    def __init__(self, code: str, message: str | None = None) -> None:
        if code not in REMOTE_CONTROL_CODES:
            raise ValueError(f"unknown remote control code {code!r}")
        super().__init__(message or code)
        self.code = code
        self.error_data: dict[str, Any] = {"code": code}


def remote_control_enabled() -> bool:
    """The ``VIBERPIT_REMOTE_CONTROL_ENABLED`` kill switch (default on)."""
    from server.config import get_settings
    return bool(get_settings().remote_control_enabled)


def eligible(*, engine: Any, kind: str | None, provider: str | None) -> bool:
    """Can a session of this engine / kind / account provider use Remote Control at all?
    The kill switch included: off, nothing is eligible."""
    from viberpit import copilot as _copilot
    from server.sessions.engine import AgentEngine
    from server.sessions.types import ASSISTANT_KIND
    return (remote_control_enabled()
            and engine is AgentEngine.CLAUDE_CODE
            and kind != ASSISTANT_KIND
            and provider != _copilot.PROVIDER)


def effective(override: bool | None, default: bool) -> bool:
    """The session's override when it has one, else the app default."""
    return bool(default) if override is None else bool(override)


def session_url(reply: Mapping[str, Any] | None) -> str | None:
    """The claude.ai session URL from an enable reply, or None."""
    if not isinstance(reply, Mapping):
        return None
    url = reply.get("session_url")
    return url if isinstance(url, str) and url else None


def control_request(enabled: bool) -> dict[str, Any]:
    """The raw control-channel request."""
    return {"subtype": "remote_control", "enabled": bool(enabled)}


def stored_default(value: bool | None) -> bool:
    """``AppSettings.remote_control_default`` as a plain bool: NULL reads as on."""
    return value is not False


# The edge's cached app default: (value, read at monotonic seconds). The session payload
# serializer is sync, so it reads this; requests that serve payloads refresh it first.
_cache: tuple[bool, float] | None = None


def cached_default() -> bool:
    """The last app default read on this process, or on (the NULL default) if none."""
    return _cache[0] if _cache is not None else True


def remember_default(value: bool) -> None:
    """Record *value* as this process's current app default (a settings write)."""
    global _cache
    _cache = (bool(value), time.monotonic())


async def read_default() -> bool:
    """``AppSettings.remote_control_default`` from the DB (NULL = on), remembered for the
    sync serializer. A failed read keeps the last value (INFO) — Remote Control is
    best-effort, so a DB hiccup must never fail a spawn or a page."""
    from sqlalchemy import select as sa_select
    from server.db import db_session
    from server.db.models import AppSettings
    try:
        async with db_session() as db:
            value = (await db.execute(sa_select(AppSettings.remote_control_default).where(
                AppSettings.id == "singleton"))).scalar_one_or_none()
    except Exception as exc:
        log.info("remote_control default not read: %s", exc)
        return cached_default()
    remember_default(stored_default(value))
    return cached_default()


async def refresh_default(max_age: float = DEFAULT_CACHE_TTL_S) -> bool:
    """``read_default`` unless the cached value is younger than *max_age* seconds."""
    if _cache is not None and time.monotonic() - _cache[1] < max_age:
        return _cache[0]
    return await read_default()
