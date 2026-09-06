"""Assistant policy: enabled/model resolution (env → AppSettings) and the route allowlist
the assistant bearer token may reach (spec §4.3, §4.11). One module so the middleware, the
API and the tool client agree."""
from __future__ import annotations

import re

from sqlalchemy import select

from server.config import get_settings
from server.db.models import AppSettings


async def _row(db) -> AppSettings | None:
    return (await db.execute(
        select(AppSettings).where(AppSettings.id == "singleton"))).scalar_one_or_none()


async def assistant_enabled(db) -> bool:
    row = await _row(db)
    if row is not None and row.assistant_enabled is not None:
        return bool(row.assistant_enabled)
    return bool(get_settings().assistant_enabled)


async def operator_tools_enabled(db) -> bool:
    """Whether the operator tier (deploy, host upgrade, account refresh/enable/reauth) is on.

    Same env → AppSettings precedence as `assistant_enabled`, but opt-in: the env default
    is false (spec v2 §7, decision B1). Callers use this at BOTH enforcement points — tool
    registration and the route allowlist — so an off setting means the model cannot even
    attempt an operator call, not merely that the call is denied.
    """
    row = await _row(db)
    if row is not None and row.assistant_operator_tools is not None:
        return bool(row.assistant_operator_tools)
    return bool(get_settings().assistant_operator_tools)


async def assistant_model(db) -> str:
    row = await _row(db)
    if row is not None and row.assistant_model is not None:
        return row.assistant_model
    return get_settings().assistant_model


# (method, path regex). Anything not listed is 403 for the assistant token, BEFORE routing.
# Keep in the same order as spec §4.2; a new tool must add its row here or its calls fail.
#
# Session ids are UUID4 strings (see `str(uuid.uuid4())` in
# server/sessions/orchestrator.py), so the id segment is matched structurally rather than
# with a loose `[A-Za-z0-9-]+` denylist-style pattern. A loose pattern would treat the
# static segments of the real router's batch/collection routes (`batch`, `search`,
# `archived`, `adopt`, `adoptable` — see server/api/sessions.py:816-819, which registers
# those routes before `/{session_id}` for exactly this reason) as a session id and
# wrongly admit routes spec §4.2 excludes, e.g. `POST /api/sessions/batch/unarchive`
# matching `_S + r"/unarchive"`, or `GET /api/sessions/adoptable` matching the bare
# `GET _S` row. A UUID4-anchored pattern can never match those static segments.
_ID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_S = r"/api/sessions/" + _ID
ASSISTANT_ROUTE_ALLOW: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (m, re.compile(p + r"$")) for m, p in [
        ("GET", r"/api/sessions"),
        ("GET", r"/api/sessions/search"),
        ("GET", r"/api/sessions/archived"),
        ("GET", _S),
        ("GET", _S + r"/transcript"),
        ("GET", _S + r"/export\.md"),
        ("GET", r"/api/accounts"),
        ("GET", r"/api/fleet/hosts"),
        ("GET", r"/api/prs"),
        ("GET", r"/api/pipeline/insights/checks"),
        ("GET", r"/api/pipeline/repos"),
        ("GET", r"/api/github/repos"),
        ("GET", r"/api/github/branches"),
        ("GET", r"/api/automations"),
        ("GET", r"/api/automations/[A-Za-z0-9_-]+/events"),
        ("GET", r"/api/deploy/status"),
        ("GET", r"/api/auth/me"),
        ("GET", r"/api/sdlc/initiatives"),
        ("GET", r"/api/sdlc/initiatives/[a-z0-9-]+"),
        ("POST", r"/api/sdlc/initiatives/[a-z0-9-]+/advance"),
        ("POST", r"/api/sessions"),
        ("POST", _S + r"/prompt"),
        ("POST", _S + r"/interrupt"),
        ("POST", _S + r"/questions/[A-Za-z0-9-]+"),
        ("POST", _S + r"/permissions/[A-Za-z0-9-]+"),
        ("POST", _S + r"/model"),
        ("POST", _S + r"/permission-mode"),
        ("POST", _S + r"/model-approval"),
        ("DELETE", _S),
        ("POST", _S + r"/unarchive"),
        ("POST", _S + r"/autopilot"),
        ("POST", _S + r"/fix-pr"),
        ("PATCH", r"/api/automations/[A-Za-z0-9_-]+"),
        ("DELETE", _S + r"/permanent"),
        ("DELETE", _S + r"/queue"),
        ("POST", r"/api/pipeline/pr/ready-merge"),
        ("GET", r"/api/notifications/channels"),
        ("GET", r"/api/notifications/rules"),
        ("POST", r"/api/notifications/rules"),
        ("DELETE", r"/api/notifications/rules/[A-Za-z0-9-]+"),
        ("GET", r"/api/notifications/quiet-hours"),
        ("PUT", r"/api/notifications/quiet-hours"),
        ("POST", r"/api/notifications/test"),
    ]
)


def route_allowed(method: str, path: str) -> bool:
    path = path.split("?", 1)[0]
    return any(m == method.upper() and rx.match(path) for m, rx in ASSISTANT_ROUTE_ALLOW)


# READ-class tools whose backing route is a POST that mutates nothing (a dry-run or a
# render). The conformance test (server/tests/test_assistant_conformance.py) lets a
# READ tool use POST only when it is listed here. Later tasks add names to this set.
READ_POSTS: frozenset[str] = frozenset({"test_notification"})
