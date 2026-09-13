"""Confirmed upstream issue decisions, optimistic conflicts and write-ahead journal."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from server.db import db_session, init_db
from server.db.models import GhWriteJournal, WorkItem
from server.pm.issue_disposition import IssueDecision, apply, detail

NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def item(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path}/issue.db"
    await init_db(url)
    async with db_session() as db:
        row = WorkItem(repo="o/r", number=5, kind="issue", title="Old", body="Description",
                       state="open", gh_node_id="N5", url="https://github.com/o/r/issues/5")
        db.add(row)
        await db.flush()
        return row.id


def node(**overrides):
    return dict(id="N5", title="Old", body="Description", state="OPEN", stateReason=None,
                url="https://github.com/o/r/issues/5", **overrides)


async def run(item, action="resolve", *, remote=None, response=None, version=None):
    fetch = AsyncMock(return_value={"data": {"node": remote or node()}})
    mutate = AsyncMock(return_value=response or {"data": {"m0": {"issue": {
        **node(), "state": "CLOSED", "stateReason": "COMPLETED"}}}})
    async with db_session() as db:
        row = await db.get(WorkItem, item)
        request = IssueDecision(action=action, version=version or detail(row)["version"],
                                title="New", body="New body")
        result = await apply(db, src=row, decision=request, fetch=fetch, mutate=mutate, now=NOW)
    return result, fetch, mutate


@pytest.mark.asyncio
@pytest.mark.parametrize("action,reason", [("resolve", "COMPLETED"), ("dismiss", "NOT_PLANNED")])
async def test_close_confirmed(item, action, reason):
    result, _, mutate = await run(item, action, response={"data": {"m0": {"issue": {
        **node(), "state": "CLOSED", "stateReason": reason}}}})
    assert result["ok"] and result["item"]["state"] == "closed"
    assert f"stateReason: {reason}" in mutate.call_args.args[1]
    async with db_session() as db:
        assert (await db.get(WorkItem, item)).state == "closed"
        journal = (await db.scalars(select(GhWriteJournal))).one()
        assert journal.applied


@pytest.mark.asyncio
async def test_edit_confirmed_and_escaped(item):
    result, _, mutate = await run(item, "edit", response={"data": {"m0": {"issue": {
        **node(), "title": "New", "body": "New body"}}}})
    assert result["item"]["body"] == "New body"
    assert "updateIssue" in mutate.call_args.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"errors": [{"message": "permission denied"}]},
    {"data": {"m0": {"issue": None}}},
    {"data": {"m0": {"issue": node()}}},
])
async def test_failure_keeps_attention(item, response):
    result, _, _ = await run(item, response=response)
    assert not result["ok"]
    async with db_session() as db:
        assert (await db.get(WorkItem, item)).state == "open"
        assert not (await db.scalars(select(GhWriteJournal))).one().applied


@pytest.mark.asyncio
async def test_remote_conflict_refreshes_mirror_without_write(item):
    result, _, mutate = await run(item, remote={**node(), "body": "Someone else edited"})
    assert result["step"] == "conflict"
    mutate.assert_not_called()
    async with db_session() as db:
        assert (await db.get(WorkItem, item)).body == "Someone else edited"


@pytest.mark.asyncio
async def test_stale_mirror_does_not_call_github(item):
    result, fetch, mutate = await run(item, version="stale")
    assert result["step"] == "conflict"
    fetch.assert_not_called()
    mutate.assert_not_called()


@pytest.mark.asyncio
async def test_transport_failure_is_journaled_before_call(item):
    async def fail(owner, doc):
        async with db_session() as other:
            assert (await other.scalars(select(GhWriteJournal))).one().applied is False
        raise RuntimeError("credential should never be echoed")
    async with db_session() as db:
        src = await db.get(WorkItem, item)
        result = await apply(db, src=src, decision=IssueDecision(action="resolve", version=detail(src)["version"]),
                            fetch=AsyncMock(return_value={"data": {"node": node()}}), mutate=fail, now=NOW)
        assert not result["ok"] and "credential" not in result["error"]
        assert src.state == "open"


@pytest.mark.asyncio
@pytest.mark.parametrize("remote", [{"data": {"node": None}}, {"errors": [{"message": "denied"}]}])
async def test_preflight_failure_never_mutates(item, remote):
    mutate = AsyncMock()
    async with db_session() as db:
        src = await db.get(WorkItem, item)
        result = await apply(db, src=src, decision=IssueDecision(action="resolve", version=detail(src)["version"]),
                            fetch=AsyncMock(return_value=remote), mutate=mutate, now=NOW)
        assert result["step"] == "fetch"
        assert src.state == "open"
    mutate.assert_not_called()


@pytest.mark.asyncio
async def test_missing_node_fails_without_transport(item):
    async with db_session() as db:
        src = await db.get(WorkItem, item)
        src.gh_node_id = None
    result, fetch, mutate = await run(item)
    assert result["step"] == "node_id"
    fetch.assert_not_called()
    mutate.assert_not_called()


@pytest.mark.asyncio
async def test_orchestrator_serializes_same_issue(item):
    import asyncio
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._pm_sync_fetch_projects = AsyncMock(return_value={"data": {"node": node()}})
    orch._pm_reconcile_mutate = AsyncMock(return_value={"data": {"m0": {"issue": {
        **node(), "state": "CLOSED", "stateReason": "COMPLETED"}}}})
    async with db_session() as db:
        version = detail(await db.get(WorkItem, item))["version"]
    results = await asyncio.gather(*[
        orch._pm_issue_decision(item, {"action": "resolve", "version": version}) for _ in range(2)])
    assert sum(bool(r["ok"]) for r in results) == 1
    assert next(r for r in results if not r["ok"])["step"] == "conflict"
    orch._pm_reconcile_mutate.assert_awaited_once()


@pytest.mark.asyncio
async def test_remote_transport_and_unavailable():
    from server.ipc.client import DaemonUnavailable
    from server.sessions.remote import RemoteOrchestrator
    client = AsyncMock()
    client.call.return_value = {"ok": True, "item": {"state": "closed"}}
    remote = RemoteOrchestrator(client)
    decision = {"action": "resolve", "version": "v"}
    assert (await remote.pm_issue_decision("id", decision))["ok"]
    assert client.call.call_args.args == ("pm_issue_decision",)
    assert client.call.call_args.kwargs["decision"] == decision
    client.call.side_effect = DaemonUnavailable("offline")
    assert (await remote.pm_issue_decision("id", decision))["step"] == "rpc"
