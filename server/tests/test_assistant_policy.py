from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from server.assistant.policy import (
    assistant_enabled, assistant_model, operator_tools_enabled, route_allowed)
from server.db.models import AppSettings


# `get_settings()` re-reads the environment on every call (it is NOT cached), so
# patch.dict is enough — there is no cache to clear.
@pytest.mark.asyncio
async def test_enabled_env_then_settings_override(isolated_db):
    from server.db import db_session
    with patch.dict(os.environ, {"CCSWITCH_ASSISTANT_ENABLED": "false"}):
        async with db_session() as db:
            assert await assistant_enabled(db) is False
            db.add(AppSettings(id="singleton", assistant_enabled=True))
            await db.commit()
            assert await assistant_enabled(db) is True


@pytest.mark.asyncio
async def test_model_default_sonnet(isolated_db, monkeypatch):
    from server.db import db_session
    monkeypatch.delenv("CCSWITCH_ASSISTANT_MODEL", raising=False)
    async with db_session() as db:
        assert await assistant_model(db) == "sonnet"


# Operator tier (spec v2 §7, decision B1): unlike `assistant_enabled` this defaults
# OFF, so the no-env / no-row case is the one that matters most — a deployment that
# never touches the setting must not get deploy/upgrade/account tools.
@pytest.mark.asyncio
async def test_operator_tools_default_off(isolated_db, monkeypatch):
    from server.db import db_session
    monkeypatch.delenv("CCSWITCH_ASSISTANT_OPERATOR_TOOLS", raising=False)
    async with db_session() as db:
        assert await operator_tools_enabled(db) is False


@pytest.mark.asyncio
async def test_operator_tools_env_then_settings_override(isolated_db):
    from server.db import db_session
    with patch.dict(os.environ, {"CCSWITCH_ASSISTANT_OPERATOR_TOOLS": "true"}):
        async with db_session() as db:
            assert await operator_tools_enabled(db) is True
            # An explicit stored False beats the env's True: the switch in Settings is
            # the operator's last word, and `is not None` (not truthiness) is what makes
            # storing False distinguishable from "unset".
            db.add(AppSettings(id="singleton", assistant_operator_tools=False))
            await db.commit()
            assert await operator_tools_enabled(db) is False


_UUID = "3f2a9c1e-7b4d-4e0a-9c7d-1a2b3c4d5e6f"


@pytest.mark.parametrize("method,path,ok", [
    ("GET", "/api/sessions", True),
    ("GET", f"/api/sessions/{_UUID}/transcript", True),
    ("POST", "/api/sessions", True),
    ("DELETE", f"/api/sessions/{_UUID}", True),
    ("DELETE", f"/api/sessions/{_UUID}/permanent", True),
    ("GET", "/api/auth/me", True),
    ("GET", "/api/sessions/search", True),
    ("GET", "/api/sessions/archived", True),
    ("GET", f"/api/sessions/{_UUID}/export.md", True),
    ("GET", "/api/github/repos", True),
    ("GET", "/api/github/branches", True),
    ("GET", "/api/pipeline/repos", True),
    # Read-only discovery: the write half of the same routers stays out.
    ("POST", "/api/github/repos", False),
    ("DELETE", "/api/github/credential-alerts", False),
    ("GET", "/api/github/credential-alerts", False),
    ("GET", "/api/assistant/session", False),
    ("POST", "/api/assistant/session", False),
    ("POST", "/api/deploy/start", False),
    ("DELETE", "/api/accounts/foo", False),
    ("PUT", "/api/settings", False),
    ("GET", "/api/settings", False),
    ("POST", "/api/auth/logout", False),
    ("POST", "/api/fleet/hosts/rocky/upgrade", False),
    ("POST", "/api/sessions/batch/permanent-delete", False),
    # Regression: static route segments must never be matched as a session id (a real
    # session id is a UUID4 — see server/api/sessions.py:816-819 for why the real router
    # registers these batch/collection routes before the dynamic /{session_id} routes).
    ("POST", "/api/sessions/batch/unarchive", False),
    ("POST", "/api/sessions/batch/archive", False),
    ("POST", "/api/sessions/batch/resume", False),
    ("GET", "/api/sessions/adoptable", False),
    ("POST", "/api/sessions/adopt", False),
    ("DELETE", "/api/sessions/abc", False),
    # Task 8.1 — notification channel/rule/quiet-hours tools.
    ("GET", "/api/notifications/channels", True),
    ("GET", "/api/notifications/rules", True),
    ("POST", "/api/notifications/rules", True),
    ("DELETE", "/api/notifications/rules/rule-1", True),
    ("GET", "/api/notifications/quiet-hours", True),
    ("PUT", "/api/notifications/quiet-hours", True),
    ("POST", "/api/notifications/test", True),
    # Regression: channels are CRUD via PUT/DELETE/POST but only GET is allowlisted
    # for the assistant — creating/editing/deleting a webhook channel is not a tool.
    ("POST", "/api/notifications/channels", False),
    ("PUT", "/api/notifications/channels/ch1", False),
    ("DELETE", "/api/notifications/channels/ch1", False),
    ("PUT", "/api/notifications/rules/rule-1", False),
    ("DELETE", "/api/notifications/rules/rule-1/extra", False),
    ("GET", "/api/notifications/test", False),
])
def test_route_allowlist(method, path, ok):
    assert route_allowed(method, path) is ok
