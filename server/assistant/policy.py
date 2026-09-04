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


async def assistant_model(db) -> str:
    row = await _row(db)
    if row is not None and row.assistant_model:
        return row.assistant_model
    return get_settings().assistant_model


# (method, path regex). Anything not listed is 403 for the assistant token, BEFORE routing.
# Keep in the same order as spec §4.2; a new tool must add its row here or its calls fail.
_S = r"/api/sessions/[A-Za-z0-9-]+"
ASSISTANT_ROUTE_ALLOW: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (m, re.compile(p + r"$")) for m, p in [
        ("GET", r"/api/sessions"),
        ("GET", r"/api/sessions/search"),
        ("GET", r"/api/sessions/archived"),
        ("GET", _S),
        ("GET", _S + r"/transcript"),
        ("GET", r"/api/accounts"),
        ("GET", r"/api/fleet/hosts"),
        ("GET", r"/api/prs"),
        ("GET", r"/api/pipeline/insights/checks"),
        ("GET", r"/api/automations"),
        ("GET", r"/api/automations/[A-Za-z0-9_-]+/events"),
        ("GET", r"/api/deploy/status"),
        ("GET", r"/api/auth/me"),
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
    ]
)


def route_allowed(method: str, path: str) -> bool:
    path = path.split("?", 1)[0]
    return any(m == method.upper() and rx.match(path) for m, rx in ASSISTANT_ROUTE_ALLOW)
