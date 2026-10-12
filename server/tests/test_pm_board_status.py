"""A person's board Status, journaled (the assistant's set_board_status, Refs #2979).

Every GitHub write goes through a fake ``mutate`` seam. Pinned: refusals before any
call, the board's REAL Status options as the vocabulary, add-then-set for an issue not
on the board, journal rows written ahead and marked, the Status row journaled under
its own label so the reconciler does not read it back as its own write, the mirror
stamped, and the sync request.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import select

from server.db import db_session, init_db
from server.db.models import (GhProject, GhProjectItem, GhWriteJournal, Project, ProjectRepo,
                              WorkItem)
from server.pm import journal
from server.pm.board_status import BoardStatusRequest, apply, refusal, status_options
from server.tests.pm_ownership_fixtures import identity_repositories, ownership_snapshot

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
CACHE = {"Status": {"field_id": "F_STATUS", "options": {
    "Backlog": "o-backlog", "Ready": "o-ready", "In progress": "o-ip", "Done": "o-done"}}}


@pytest.fixture(autouse=True)
def canonical_mutation_repositories(monkeypatch):
    monkeypatch.setattr("server.automations.target_identity.resolve_repositories",
                        identity_repositories)


@pytest.fixture
def db_env():
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite+aiosqlite:///{tmp}/t.db"
        with patch.dict(os.environ, {"VIBERPIT_DB_URL": url, "VIBERPIT_DEV": "1",
                                     "VIBERPIT_TAILNET_ALLOWLIST": ""}):
            yield url


async def _seed(db_url: str, *, on_board: str | None = "Backlog", **issue) -> str:
    await init_db(db_url)
    async with db_session() as db:
        p = Project(slug="billing", name="billing", onboarding_state="live")
        db.add(p)
        await db.flush()
        db.add(ProjectRepo(project_id=p.id, repo="o/r", is_primary=True))
        gh = GhProject(project_id=p.id, gh_node_id="PVT_1", field_cache=CACHE)
        db.add(gh)
        fields = dict(repo="o/r", number=5, kind="issue", title="Proration", state="open",
                      gh_node_id="I_5")
        fields.update(issue)
        wi = WorkItem(**fields)
        db.add(wi)
        await db.flush()
        if on_board is not None:
            db.add(GhProjectItem(gh_project_id=gh.id, work_item_id=wi.id,
                                 gh_item_node_id="PVTI_5", status=on_board))
        return wi.id


def _mutate(*payloads: dict, calls: list):
    queue = list(payloads)

    async def mutate(owner: str, doc: str) -> dict:
        calls.append((owner, doc))
        return queue.pop(0)
    return mutate


SET_OK = {"data": {"m0": {"projectV2Item": {"id": "PVTI_5"}}}}


async def _run(wid: str, mutate, **request):
    async with db_session() as db:
        src = await db.get(WorkItem, wid)
        assert src is not None
        result = await apply(db, src=src, request=BoardStatusRequest(**request), mutate=mutate,
                             now=NOW, ownership=await ownership_snapshot(db, "o/r"))
        await db.commit()
        return result


@pytest.mark.asyncio
async def test_sets_status_on_an_item_already_on_the_board(db_env):
    wid = await _seed(db_env)
    calls: list = []
    result = await _run(wid, _mutate(SET_OK, calls=calls), status="Ready")
    assert result == {"ok": True, "ref": "o/r#5", "status": "Ready", "previous": "Backlog",
                      "added": False, "changed": True}
    assert len(calls) == 1 and calls[0][0] == "o"
    assert ('updateProjectV2ItemFieldValue(input: {projectId: "PVT_1", itemId: "PVTI_5", '
            'fieldId: "F_STATUS", value: {singleSelectOptionId: "o-ready"}})') in calls[0][1]
    async with db_session() as db:
        row = (await db.execute(select(GhWriteJournal))).scalar_one()
        assert (row.field, row.applied, row.error) == (journal.REQUESTED_STATUS_FIELD, True, None)
        assert row.new_value == {"value": "Ready"}
        item = (await db.execute(select(GhProjectItem))).scalar_one()
        assert item.status == "Ready"
        assert item.gh_updated_at is not None
        assert (await db.execute(select(ProjectRepo))).scalar_one().sync_requested_at is not None
        # The reconciler's "this value is ours" read must NOT claim the person's write,
        # or its next cycle would push its own decision straight back over it.
        assert await db.run_sync(journal.own_applied_values) == {}


@pytest.mark.asyncio
async def test_adds_an_issue_not_on_the_board_then_sets_status(db_env):
    wid = await _seed(db_env, on_board=None)
    calls: list = []
    added = {"data": {"m0": {"item": {"id": "PVTI_NEW"}}}}
    result = await _run(wid, _mutate(added, SET_OK, calls=calls), status="Ready")
    assert result["ok"] and result["added"] and result["previous"] is None
    assert 'addProjectV2ItemById(input: {projectId: "PVT_1", contentId: "I_5"})' in calls[0][1]
    assert 'itemId: "PVTI_NEW"' in calls[1][1]
    async with db_session() as db:
        rows = (await db.execute(select(GhWriteJournal).order_by(GhWriteJournal.created_at))).scalars().all()
        assert sorted((r.field, r.applied) for r in rows) == [
            (journal.REQUESTED_STATUS_FIELD, True), ("add_item", True)]
        item = (await db.execute(select(GhProjectItem))).scalar_one()
        assert (item.gh_item_node_id, item.work_item_id, item.status) == ("PVTI_NEW", wid, "Ready")


@pytest.mark.asyncio
async def test_not_on_board_without_add_is_refused_before_any_call(db_env):
    wid = await _seed(db_env, on_board=None)
    calls: list = []
    result = await _run(wid, _mutate(calls=calls), status="Ready", add_if_missing=False)
    assert result["step"] == "not_on_board" and calls == []


@pytest.mark.asyncio
async def test_unknown_status_names_the_boards_real_options(db_env):
    wid = await _seed(db_env)
    calls: list = []
    result = await _run(wid, _mutate(calls=calls), status="Someday")
    assert result["ok"] is False and result["step"] == "status"
    assert "Backlog, Ready, In progress, Done" in result["error"]
    assert calls == []
    async with db_session() as db:
        assert (await db.execute(select(GhWriteJournal))).scalars().all() == []


@pytest.mark.asyncio
async def test_same_status_is_a_no_op(db_env):
    wid = await _seed(db_env, on_board="Ready")
    calls: list = []
    result = await _run(wid, _mutate(calls=calls), status="Ready")
    assert result["ok"] and result["changed"] is False and calls == []


@pytest.mark.asyncio
async def test_github_refusal_marks_the_row_failed_and_keeps_the_mirror(db_env):
    wid = await _seed(db_env)
    payload = {"data": {"m0": None}, "errors": [{"path": ["m0"], "message": "no access"}]}
    result = await _run(wid, _mutate(payload, calls=[]), status="Ready")
    assert result == {"ok": False, "step": "mutate", "error": "no access", "added": False}
    async with db_session() as db:
        row = (await db.execute(select(GhWriteJournal))).scalar_one()
        assert (row.applied, row.error) == (False, "no access")
        assert (await db.execute(select(GhProjectItem))).scalar_one().status == "Backlog"


@pytest.mark.asyncio
async def test_transport_failure_is_not_echoed(db_env):
    wid = await _seed(db_env)

    async def boom(owner, doc):
        raise RuntimeError("token ghp_secret leaked")
    result = await _run(wid, boom, status="Ready")
    assert result["step"] == "mutate" and "ghp_secret" not in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("issue,needle", [
    ({"kind": "pr"}, "pull request"),
    ({"state": "closed"}, "is closed"),
    ({"title": "Nightly failed — 2026-10-09"}, "tracker"),
    ({"labels": ["red-watchdog"]}, "tracker"),
    ({"hold_kind": "owner_action"}, "held"),
])
async def test_refusals_never_reach_github(db_env, issue, needle):
    wid = await _seed(db_env, **issue)
    calls: list = []
    result = await _run(wid, _mutate(calls=calls), status="Ready")
    assert result["ok"] is False and result["step"] == "refused"
    assert needle in result["error"] and calls == []


@pytest.mark.asyncio
async def test_unregistered_repo_is_an_ownership_refusal(db_env):
    wid = await _seed(db_env, repo="other/x")
    async with db_session() as db:
        src = await db.get(WorkItem, wid)
        result = await apply(db, src=src, request=BoardStatusRequest(status="Ready"),
                             mutate=_mutate(calls=[]), now=NOW,
                             ownership=await ownership_snapshot(db, "other/x"))
    assert result["ok"] is False and result["step"] == "ownership"


def test_status_options_and_refusal_helpers():
    assert status_options(None) == []
    assert status_options(GhProject(field_cache=CACHE))[:2] == ["Backlog", "Ready"]
    assert refusal(WorkItem(repo="o/r", number=1, kind="issue", state="open", title="x")) is None


@pytest.mark.asyncio
async def test_orchestrator_seam_and_remote_transport(db_env):
    from unittest.mock import AsyncMock

    from server.ipc.client import DaemonUnavailable
    from server.sessions.orchestrator import SessionOrchestrator
    from server.sessions.remote import RemoteOrchestrator
    wid = await _seed(db_env)
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._pm_reconcile_mutate = AsyncMock(return_value=SET_OK)
    orch._pm_sync_now_utc = lambda: NOW
    with patch("server.pm.repo_project.gather_repo_ownership_snapshot", new=_snapshot):
        result = await orch._pm_board_status(wid, {"status": "Ready", "add_if_missing": True})
    assert result["ok"] and result["status"] == "Ready"
    assert (await orch._pm_board_status("nope", {"status": "Ready"}))["step"] == "load"

    client = AsyncMock()
    client.call.return_value = {"ok": True}
    remote = RemoteOrchestrator(client)
    assert (await remote.pm_board_status("id", {"status": "Ready"}))["ok"]
    assert client.call.call_args.args == ("pm_board_status",)
    assert client.call.call_args.kwargs["request"] == {"status": "Ready"}
    client.call.side_effect = DaemonUnavailable("offline")
    assert (await remote.pm_board_status("id", {"status": "Ready"}))["step"] == "rpc"


async def _snapshot(repos):
    async with db_session() as db:
        return await ownership_snapshot(db, *repos)
