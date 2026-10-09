import pytest

from server.db import db_session
from server.db.models import Session as DbSession
from server.mcp_scope import store


@pytest.fixture
def make_session_row():
    async def _make(sid: str = "s-mcp-1") -> str:
        async with db_session() as db:
            db.add(DbSession(id=sid, name="n", account_name="acct", cwd="/tmp"))
        return sid
    return _make


@pytest.mark.asyncio
async def test_defaults_roundtrip_and_upsert(isolated_db):
    await store.set_default("gh", False)
    await store.set_default("gh", True)
    assert await store.server_defaults() == {"gh": True}


@pytest.mark.asyncio
async def test_repo_rules_match_case_insensitively(isolated_db):
    await store.set_repo_rule("Owner/Repo", "gh", True)
    assert await store.repo_rules("owner/repo") == {"gh": True}
    assert await store.repo_rules("OWNER/REPO") == {"gh": True}
    await store.set_repo_rule("owner/REPO", "gh", False)  # same key, upsert
    assert await store.all_rules() == [{"repo": "owner/repo", "server": "gh", "enabled": False}]


@pytest.mark.asyncio
async def test_delete_repo_rule_reports_whether_it_existed(isolated_db):
    await store.set_repo_rule("a/b", "gh", True)
    assert await store.delete_repo_rule("A/B", "gh") is True
    assert await store.delete_repo_rule("a/b", "gh") is False


@pytest.mark.asyncio
async def test_delete_server_drops_default_and_rules(isolated_db):
    await store.set_default("gh", False)
    await store.set_repo_rule("a/b", "gh", True)
    await store.set_repo_rule("a/b", "other", True)
    await store.delete_server("gh")
    assert await store.server_defaults() == {}
    assert await store.all_rules() == [{"repo": "a/b", "server": "other", "enabled": True}]


@pytest.mark.asyncio
async def test_session_override_merges(isolated_db, make_session_row):
    sid = await make_session_row()
    assert await store.session_overrides(sid) == {}
    assert await store.set_session_override(sid, "gh", False) == {"gh": False}
    assert await store.set_session_override(sid, "plugin:p:s", True) == {"gh": False, "plugin:p:s": True}
    assert await store.session_overrides(sid) == {"gh": False, "plugin:p:s": True}
    assert await store.session_overrides("missing") == {}
