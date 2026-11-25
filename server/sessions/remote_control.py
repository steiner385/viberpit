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
#: A live token rotation (``update_environment_variables``) is answered at once.
TOKEN_PUSH_TIMEOUT_S = 10.0

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
#: Why a session that is eligible on paper still cannot open a bridge — learned from the
#: CLI's own refusal at runtime (Principle III), never predicted. ``login_rejected``: the
#: account signs in with an inference-only setup-token (``CLAUDE_CODE_OAUTH_TOKEN``),
#: which claude.ai refuses for Remote Control. ``refused``: any other CLI refusal.
#: ``login_rejected`` is now learned only from a session that launched on the account's
#: full claude.ai login (the slot) and was still refused.
REASON_LOGIN_REJECTED = "login_rejected"
REASON_REFUSED = "refused"
#: Known before any request: the session launched on the account's inference-only
#: setup-token because the account has no usable claude.ai login (``accounts.slot_status``
#: is not ok, or its org is unknown). Cleared once the login is back.
REASON_LOGIN_NEEDED = "login_needed"
#: Known before any request: the session runs on a session runner, which holds no
#: claude.ai login of its own (follow-up: vend one through the credential envelope).
REASON_RUNNER = "runner"
#: Known before any request: the login is fine but its access token is stale and a
#: live session on the account owns the login's token family, so it may not be
#: refreshed now. Lifts on its own once that session is gone or the token is fresh.
REASON_LOGIN_BUSY = "login_busy"
LOGIN_REJECTED_MESSAGE = ("claude.ai rejected this account's login for Remote Control. "
                          "Sign in to claude.ai again (Accounts → Sign in to claude.ai).")
REFUSED_MESSAGE = "The Claude CLI refused Remote Control for this account."
LOGIN_NEEDED_MESSAGE = ("Sign in to claude.ai for this account (Accounts → Sign in to "
                        "claude.ai) to use Remote Control")
LOGIN_BUSY_MESSAGE = ("This account's login is in use by another session; try again "
                      "when it's idle.")
RUNNER_MESSAGE = "Remote Control is not available yet for sessions on a session runner."
BUSY_MESSAGE = ("Remote Control restarts this session on the account's claude.ai login; "
                "try again once the current turn finishes.")

#: Turning it on needs a respawn onto the account's claude.ai login, and the session is
#: mid-turn (or another swap is in flight).
REMOTE_CONTROL_BUSY = "REMOTE_CONTROL_BUSY"

REMOTE_CONTROL_CODES = frozenset({REMOTE_CONTROL_UNAVAILABLE, REMOTE_CONTROL_NOT_LIVE,
                                  REMOTE_CONTROL_REFUSED, REMOTE_CONTROL_FAILED,
                                  REMOTE_CONTROL_BUSY})

_REASON_MESSAGES = {REASON_LOGIN_REJECTED: LOGIN_REJECTED_MESSAGE,
                    REASON_LOGIN_NEEDED: LOGIN_NEEDED_MESSAGE,
                    REASON_LOGIN_BUSY: LOGIN_BUSY_MESSAGE,
                    REASON_RUNNER: RUNNER_MESSAGE}

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


def effective(override: bool | None, default: bool | None) -> bool:
    """The session's override when it has one, else the app default. An UNKNOWN default
    (None: never read on this process) is off — fail closed; a stored NULL is not
    unknown, it reads as on (``stored_default``)."""
    return default is True if override is None else bool(override)


def refusal_reason(message: str | None) -> str:
    """The unavailability reason a CLI refusal's *message* stands for."""
    return REASON_LOGIN_REJECTED if "login" in (message or "").lower() else REASON_REFUSED


def refusal_message(reason: str) -> str:
    """The plain-words message a toggle refused for *reason* carries."""
    return _REASON_MESSAGES.get(reason, REFUSED_MESSAGE)


#: ``remoteControlState`` in the session payload — what the UI may honestly claim.
STATE_ON = "on"                    # effectively on AND the bridge answered with its URL
STATE_CONNECTING = "connecting"    # effectively on, no bridge yet: the enable is in
                                   # flight, or the session still runs on the setup-token
                                   # and waits to be respawned onto the login
STATE_OFF = "off"                  # effectively off
STATE_UNAVAILABLE = "unavailable"  # not eligible, or a known reason it cannot open one


def display_state(*, available: bool, reason: str | None, effective_on: bool,
                  url: str | None) -> str:
    """The session's Remote Control state as the payload reports it. "on" only with a
    bridge URL — an effectively-on session without one is "connecting", never "on"."""
    if not available or reason is not None:
        return STATE_UNAVAILABLE
    if not effective_on:
        return STATE_OFF
    return STATE_ON if url else STATE_CONNECTING


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


def cached_default() -> bool | None:
    """The last app default read on this process, or None when nothing has been read
    yet (unknown — callers treat it as off)."""
    return _cache[0] if _cache is not None else None


def remember_default(value: bool) -> None:
    """Record *value* as this process's current app default (a settings write)."""
    global _cache
    _cache = (bool(value), time.monotonic())


async def read_default() -> bool:
    """``AppSettings.remote_control_default`` from the DB (NULL = on), remembered for the
    sync serializer. A failed read keeps the last known value, and with none FAILS
    CLOSED (off): an unknown default must never open a bridge to the Claude apps. Logged
    at INFO either way — Remote Control is best-effort, so a DB hiccup never fails a
    spawn or a page."""
    from sqlalchemy import select as sa_select
    from server.db import db_session
    from server.db.models import AppSettings
    try:
        async with db_session() as db:
            value = (await db.execute(sa_select(AppSettings.remote_control_default).where(
                AppSettings.id == "singleton"))).scalar_one_or_none()
    except Exception as exc:
        known = cached_default()
        log.info("remote_control default not read (using %s): %s",
                 "last known value" if known is not None else "off, fail closed", exc)
        return known is True
    remember_default(stored_default(value))
    return stored_default(value)


async def refresh_default(max_age: float = DEFAULT_CACHE_TTL_S) -> bool:
    """``read_default`` unless the cached value is younger than *max_age* seconds."""
    if _cache is not None and time.monotonic() - _cache[1] < max_age:
        return _cache[0]
    return await read_default()
