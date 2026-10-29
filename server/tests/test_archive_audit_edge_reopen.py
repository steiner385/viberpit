"""Archive Audit reopens of worker-owned sessions go through the edge (#3142).

The primary daemon holds no mTLS client identity (only the edge does, and workers
allowlist the edge alone), so it cannot dial a worker to unarchive a session there.

Covers:
  - the daemon loop hands an unreachable-owner reopen to the edge (keeps the
    "reopened" decision, flags the finding) instead of reverting it to a human.
  - the edge pass restores flagged findings through the routed unarchive: success
    clears the flag, an unreachable host retries next pass, any other failure
    reverts the finding to open for a human.
  - an operator's "reopen" is served by the edge with its own routing; other
    automation actions still go to the primary.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from server.automations.archive_audit import (
    mark_auto_reopen_failed, mark_awaiting_edge_unarchive, persist_findings,
    run_edge_unarchive_pass)
from server.db import db_session
from server.db.models import ArchiveReviewFinding
from server.ipc.client import DaemonUnavailable
from server.sessions.orchestrator import ArchiveOwnerUnreachableError


def _r(sid):
    return {"id": sid, "bucket": "premature", "reason": "open PR, not armed",
            "goal": f"goal {sid}", "action": "none", "repo": "o/r"}


async def _rows():
    async with db_session() as db:
        return {r.session_id: r
                for r in (await db.execute(select(ArchiveReviewFinding))).scalars()}


async def _reopened_and_flagged(*sids):
    await persist_findings(db_session, [_r(s) for s in sids], "auto_reopen")
    for sid in sids:
        await mark_awaiting_edge_unarchive(db_session, sid)


# --- daemon loop -------------------------------------------------------------

def test_loop_hands_an_unreachable_owner_to_the_edge(caplog):
    from server.automations.archive_audit import ArchiveAuditLoop
    from server.tests.test_archive_audit_loop import (
        _FIXED_EPOCH, _FakeCtx, _fake_record, _gh_returning, _pr, _row)

    ctx = _FakeCtx()
    mark_failed = AsyncMock()
    defer = AsyncMock()

    async def read_config():
        return ("auto_reopen", {"window_days": 7})

    loop = ArchiveAuditLoop(
        ctx, read_config=read_config,
        list_rows=AsyncMock(return_value=[_row("sess-rocky", "fix ci")]),
        gh_fn=_gh_returning(_pr(state="OPEN", auto_merge=False)), gh_num_fn=None,
        unarchive_fn=AsyncMock(side_effect=ArchiveOwnerUnreachableError(
            "sess-rocky", "rocky", "no client cert")),
        clock=lambda: _FIXED_EPOCH, record_findings_fn=_fake_record,
        mark_auto_reopen_failed_fn=mark_failed, defer_to_edge_fn=defer)
    with caplog.at_level(logging.INFO, logger="server.automations.archive_audit"):
        asyncio.run(loop.tick())

    defer.assert_awaited_once_with("sess-rocky")
    mark_failed.assert_not_awaited()
    detail = ctx.emit.call_args.kwargs["detail"]
    assert detail["unarchived"] == [] and detail["deferred_to_edge"] == ["sess-rocky"]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_loop_falls_back_to_a_human_when_the_hand_off_fails():
    from server.automations.archive_audit import ArchiveAuditLoop
    from server.tests.test_archive_audit_loop import (
        _FIXED_EPOCH, _FakeCtx, _fake_record, _gh_returning, _pr, _row)

    mark_failed = AsyncMock()

    async def read_config():
        return ("auto_reopen", {"window_days": 7})

    loop = ArchiveAuditLoop(
        _FakeCtx(), read_config=read_config,
        list_rows=AsyncMock(return_value=[_row("sess-rocky", "fix ci")]),
        gh_fn=_gh_returning(_pr(state="OPEN", auto_merge=False)), gh_num_fn=None,
        unarchive_fn=AsyncMock(side_effect=ArchiveOwnerUnreachableError(
            "sess-rocky", "rocky", "no client cert")),
        clock=lambda: _FIXED_EPOCH, record_findings_fn=_fake_record,
        mark_auto_reopen_failed_fn=mark_failed,
        defer_to_edge_fn=AsyncMock(side_effect=RuntimeError("db down")))
    asyncio.run(loop.tick())
    mark_failed.assert_awaited_once_with("sess-rocky")


# --- finding state -----------------------------------------------------------

def test_mark_awaiting_edge_only_flags_a_reopened_finding(isolated_db):
    async def run():
        await persist_findings(db_session, [_r("open1")], "propose")
        await mark_awaiting_edge_unarchive(db_session, "open1")
        await _reopened_and_flagged("s1")
        rows = await _rows()
        assert rows["open1"].awaiting_edge_unarchive is None
        assert rows["s1"].status == "reopened" and rows["s1"].awaiting_edge_unarchive is True
    asyncio.run(run())


def test_edge_pass_restores_retries_and_reverts(isolated_db):
    unarchived: list[str] = []

    async def unarchive(sid):
        if sid == "down":
            raise DaemonUnavailable("connect refused")
        if sid == "broken":
            raise RuntimeError("archive missing")
        unarchived.append(sid)
        return SimpleNamespace(id=sid)

    async def run():
        await _reopened_and_flagged("ok", "down", "broken")
        # A reopened finding NOT handed to the edge is none of the pass's business.
        await persist_findings(db_session, [_r("ok"), _r("down"), _r("broken"),
                                            _r("local")], "auto_reopen")
        out = await run_edge_unarchive_pass(db_session, unarchive)
        assert out == {"unarchived": ["ok"], "retry": ["down"], "failed": ["broken"]}
        rows = await _rows()
        assert rows["ok"].status == "reopened" and rows["ok"].awaiting_edge_unarchive is None
        assert rows["down"].status == "reopened" and rows["down"].awaiting_edge_unarchive
        assert rows["broken"].status == "open" and rows["broken"].auto_reopen_failed
        assert rows["broken"].awaiting_edge_unarchive is None
        assert unarchived == ["ok"]
        # A second pass only retries the unreachable one.
        out = await run_edge_unarchive_pass(db_session, unarchive)
        assert out == {"unarchived": [], "retry": ["down"], "failed": []}
    asyncio.run(run())


def test_reverting_a_flagged_finding_clears_the_flag(isolated_db):
    async def run():
        await _reopened_and_flagged("s1")
        await mark_auto_reopen_failed(db_session, "s1")
        row = (await _rows())["s1"]
        assert row.status == "open" and row.awaiting_edge_unarchive is None
    asyncio.run(run())


# --- edge operator reopen ----------------------------------------------------

class _Primary:
    def __init__(self):
        self.triggers: list[tuple] = []

    async def automation_trigger(self, automation_id, item_id, kind, identity=None):
        self.triggers.append((automation_id, item_id, kind, identity))
        return {"status": "primary"}


def _fleet(primary, unarchive):
    from server.sessions.fleet_orchestrator import FleetOrchestrator
    from server.tests.test_fleet_orchestrator import _FakeRouter
    fo = FleetOrchestrator(_FakeRouter({"dobby": primary}), primary="dobby")
    fo.unarchive = unarchive  # the per-session-routed call, stubbed
    return fo


@pytest.mark.parametrize("result,status,final", [
    (SimpleNamespace(id="s1"), "reopened", "reopened"),
    (None, "unarchive_failed", "open"),
    (DaemonUnavailable("rocky is off"), "unarchive_failed", "open"),
])
def test_edge_serves_an_operator_reopen_with_its_own_routing(isolated_db, result, status,
                                                              final):
    primary = _Primary()
    calls: list[str] = []

    async def unarchive(sid):
        calls.append(sid)
        if isinstance(result, Exception):
            raise result
        return result

    async def run():
        await persist_findings(db_session, [_r("s1")], "propose")
        fid = (await _rows())["s1"].id
        out = await _fleet(primary, unarchive).automation_trigger(
            "archive-audit", fid, "reopen", identity="tony")
        assert out["status"] == status
        assert (await _rows())["s1"].status == final

    asyncio.run(run())
    assert calls == ["s1"] and primary.triggers == []


def test_other_automation_actions_still_go_to_the_primary():
    primary = _Primary()

    async def unarchive(sid):
        raise AssertionError("retain must not unarchive")

    fo = _fleet(primary, unarchive)
    assert asyncio.run(fo.automation_trigger(
        "archive-audit", "f1", "retain", identity="tony")) == {"status": "primary"}
    assert asyncio.run(fo.automation_trigger(
        "cicd-doctor", "f2", "file_issue")) == {"status": "primary"}
    assert primary.triggers == [("archive-audit", "f1", "retain", "tony"),
                                ("cicd-doctor", "f2", "file_issue", None)]


def test_edge_reopen_pass_uses_the_routed_unarchive(isolated_db):
    primary = _Primary()
    calls: list[str] = []

    async def unarchive(sid):
        calls.append(sid)
        return SimpleNamespace(id=sid)

    async def run():
        await _reopened_and_flagged("s1")
        return await _fleet(primary, unarchive).archive_reopen_once()

    assert asyncio.run(run())["unarchived"] == ["s1"] and calls == ["s1"]


def test_the_edge_lifespan_starts_and_reaps_the_reopen_loop():
    import inspect

    import server.app as app_mod
    src = inspect.getsource(app_mod)
    assert "archive_reopen_loop()" in src
    assert '("fleet_archive_reopen_task", "fleet-archive-reopen")' in src
