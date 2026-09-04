from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from server.assistant.policy import (assistant_enabled, assistant_model, route_allowed)
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


@pytest.mark.parametrize("method,path,ok", [
    ("GET", "/api/sessions", True),
    ("GET", "/api/sessions/abc/transcript", True),
    ("POST", "/api/sessions", True),
    ("DELETE", "/api/sessions/abc", True),
    ("DELETE", "/api/sessions/abc/permanent", True),
    ("GET", "/api/auth/me", True),
    ("GET", "/api/assistant/session", False),
    ("POST", "/api/assistant/session", False),
    ("POST", "/api/deploy/start", False),
    ("DELETE", "/api/accounts/foo", False),
    ("PUT", "/api/settings", False),
    ("GET", "/api/settings", False),
    ("POST", "/api/auth/logout", False),
    ("POST", "/api/fleet/hosts/rocky/upgrade", False),
    ("POST", "/api/sessions/batch/permanent-delete", False),
])
def test_route_allowlist(method, path, ok):
    assert route_allowed(method, path) is ok
