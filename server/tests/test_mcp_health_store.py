"""mcp_server_status persistence (spec 2026-10-09)."""
from __future__ import annotations

import pytest

from server.mcp_health import store
from server.mcp_health.status import McpServerState as S


@pytest.mark.asyncio
async def test_upsert_overwrites_same_key(isolated_db):
    await store.upsert_states("alice", "dobby", [S("gh", "needs-auth", None, None, "user")], source="session", session_id="s1")
    await store.upsert_states("alice", "dobby", [S("gh", "connected", None, 3, "user")], source="probe", session_id=None)
    [r] = await store.rows()
    assert (r["status"], r["toolCount"], r["source"], r["sessionId"]) == ("connected", 3, "probe", None)
    assert r["observedAt"].endswith("Z")


@pytest.mark.asyncio
async def test_hosts_are_distinct_keys(isolated_db):
    await store.upsert_states("alice", "dobby", [S("x", "connected", None, 1, "user")], source="probe", session_id=None)
    await store.upsert_states("alice", "rocky", [S("x", "failed", "ENOENT", None, "user")], source="probe", session_id=None)
    assert sorted(r["host"] for r in await store.rows("x")) == ["dobby", "rocky"]


@pytest.mark.asyncio
async def test_needs_auth_and_login_key_use_short_names(isolated_db):
    await store.upsert_states("bob", "dobby", [S("plugin:railway:railway", "needs-auth", None, None, "user")], source="session", session_id="s")
    await store.upsert_states("al", "dobby", [S("plugin:railway:railway", "needs-auth", None, None, "user")], source="session", session_id="s")
    assert await store.needs_auth_by_server() == {"railway": ["al", "bob"]}
    assert await store.login_key_for("railway") == "plugin:railway:railway"
    assert await store.login_key_for("custom") == "custom"


@pytest.mark.asyncio
async def test_empty_states_is_a_noop(isolated_db):
    await store.upsert_states("alice", "dobby", [], source="session", session_id="s")
    assert await store.rows() == []


@pytest.mark.asyncio
async def test_orchestrator_persist_mcp_status_writes_row_for_record_account(isolated_db):
    from server.hostname import self_host_name
    from server.sessions.engine import AgentEngine
    from server.sessions.orchestrator import SessionOrchestrator
    from server.sessions.types import SessionRecord

    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {"sid": SessionRecord(id="sid", name="S", account="alice", engine=AgentEngine.CLAUDE_CODE,
                                          engine_session_id="cs", cols=80, rows=24, cwd="/tmp")}
    await orch._persist_mcp_status("sid", [S("gh", "connected", None, 1, "user")])
    await orch._persist_mcp_status("unknown", [S("zz", "connected", None, 1, "user")])  # no record: ignored
    [r] = await store.rows()
    assert (r["account"], r["host"], r["sessionId"], r["source"]) == ("alice", self_host_name(), "sid", "session")


@pytest.mark.asyncio
async def test_duplicate_names_in_one_call_last_wins(isolated_db):
    await store.upsert_states("alice", "dobby", [S("gh", "failed", "x", None, "user"), S("gh", "connected", None, 2, "user")],
                              source="probe", session_id=None)
    [r] = await store.rows()
    assert (r["status"], r["toolCount"], r["error"]) == ("connected", 2, None)


@pytest.mark.asyncio
async def test_concurrent_upserts_same_key_do_not_collide(isolated_db):
    import asyncio
    await asyncio.gather(*[
        store.upsert_states("alice", "dobby", [S("gh", "connected", None, i, "user")], source="session", session_id=f"s{i}")
        for i in range(8)])
    assert len(await store.rows()) == 1
