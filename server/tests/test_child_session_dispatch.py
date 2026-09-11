"""dispatch_child_session + parent notification (child-session spec §4.3, §4.4)."""
import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest

from server.sessions.child_sessions import ChildSessionLimit
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.test_assistant_orchestrator import _make_orch, _stop_orch


def _run(coro):
    return asyncio.run(coro)


async def _settle():
    for _ in range(30):          # notices are spawned tasks
        await asyncio.sleep(0.01)


def test_dispatch_creates_a_linked_child_with_narrowed_mode(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'a.db'}", tmp_path=tmp_path)
        try:
            parent = await orch.create_session(name="Parent", cwd=str(tmp_path), identity="u@x",
                                               permission_mode="auto")
            out = await orch.dispatch_child_session(parent.id, name="Child A", prompt="do X",
                                                    model=None, permission_mode="full-auto",
                                                    identity="u@x")
            child = orch._records[out["id"]]
            assert out["name"] == "Child A" and out["url"] == f"/sessions/{child.id}"
            assert child.parent_session_id == parent.id
            assert child.permission_mode == "auto"          # never wider than the parent
            assert child.name_is_auto is False
            assert orch.child_depth(child.id) == 1 and orch.child_depth(parent.id) == 0
            assert [c.id for c in orch.children_of(parent.id)] == [child.id]
            from sqlalchemy import select
            from server.db import db_session
            from server.db.models import Session as DbSession
            async with db_session() as db:
                row = (await db.execute(select(DbSession).where(DbSession.id == child.id))).scalar_one()
                assert row.parent_session_id == parent.id
        finally:
            await _stop_orch(orch)
    _run(run())


def test_dispatch_enforces_cap_depth_and_switch(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'b.db'}", tmp_path=tmp_path,
                                max_active_sessions=20)
        try:
            parent = await orch.create_session(name="P", cwd=str(tmp_path), identity="u@x")
            with patch.dict(os.environ, {"CCSWITCH_CHILD_SESSION_CAP": "1",
                                         "CCSWITCH_CHILD_SESSION_MAX_DEPTH": "1"}):
                c1 = await orch.dispatch_child_session(parent.id, name="child one", prompt="x",
                                                       model=None, permission_mode=None, identity="u@x")
                # The fake SDK answers the child's initial prompt instantly, so the child
                # is already `done` (not live). Pin it live: the cap counts live children.
                orch._records[c1["id"]].status = "in_progress"
                with pytest.raises(ChildSessionLimit, match="cap 1"):
                    await orch.dispatch_child_session(parent.id, name="child two", prompt="x",
                                                      model=None, permission_mode=None, identity="u@x")
                with pytest.raises(ChildSessionLimit, match="depth 1"):
                    await orch.dispatch_child_session(c1["id"], name="grandchild", prompt="x",
                                                      model=None, permission_mode=None, identity="u@x")
            with patch.dict(os.environ, {"CCSWITCH_CHILD_SESSIONS_ENABLED": "false"}):
                with pytest.raises(ChildSessionLimit, match="disabled"):
                    await orch.dispatch_child_session(parent.id, name="child three", prompt="x",
                                                      model=None, permission_mode=None, identity="u@x")
        finally:
            await _stop_orch(orch)
    _run(run())


def test_child_stop_notifies_parent_once_per_transition(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}", tmp_path=tmp_path,
                                max_active_sessions=20)
        try:
            parent = await orch.create_session(name="P", cwd=str(tmp_path), identity="u@x")
            out = await orch.dispatch_child_session(parent.id, name="child", prompt="x", model=None,
                                                    permission_mode=None, identity="u@x")
            # The fake SDK answers the child's initial prompt at once, so the child's
            # first real `done` (the one production notice for "first stop") lands
            # before this test's sequence. Let it land, then start from a clean guard
            # with the child live so the sequence below is the one under test.
            await _settle()
            orch._records[out["id"]].parent_notified_status = None
            orch._records[out["id"]].status = "in_progress"
            sent: list[tuple[str, str]] = []

            async def fake_send(sid, text, *a, **k):
                sent.append((sid, text)); return "sent"
            orch.send_prompt = fake_send                                   # type: ignore[method-assign]
            orch.ensure_driver = AsyncMock(return_value=object())          # type: ignore[method-assign]
            with patch.object(SessionOrchestrator, "_last_assistant_prose", return_value="all done"):
                await orch._persist_status(out["id"], "done")
                await orch._persist_status(out["id"], "done")      # same status → no second notice
                await orch._persist_status(out["id"], "in_progress")
                await orch._persist_status(out["id"], "waiting_question")
                await _settle()
            assert [s for s, _ in sent] == [parent.id, parent.id]
            assert "<status>done</status>" in sent[0][1] and "<summary>all done</summary>" in sent[0][1]
            assert "<status>waiting_question</status>" in sent[1][1]
            assert orch._records[out["id"]].parent_notified_status == "waiting_question"
        finally:
            await _stop_orch(orch)
    _run(run())


def test_done_latches_and_archiving_the_child_notifies(tmp_path):
    """Steering a child (in_progress → done again) stays silent after the first stop;
    archive_session — which never reaches _persist_status — still tells the parent."""
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'e.db'}", tmp_path=tmp_path,
                                max_active_sessions=20)
        try:
            parent = await orch.create_session(name="P", cwd=str(tmp_path), identity="u@x")
            out = await orch.dispatch_child_session(parent.id, name="child", prompt="x", model=None,
                                                    permission_mode=None, identity="u@x")
            await _settle()
            child = orch._records[out["id"]]
            child.parent_notified_status = "done"        # first stop already reported
            child.status = "in_progress"
            sent: list[tuple[str, str]] = []

            async def fake_send(sid, text, *a, **k):
                sent.append((sid, text)); return "sent"
            orch.send_prompt = fake_send                                   # type: ignore[method-assign]
            orch.ensure_driver = AsyncMock(return_value=object())          # type: ignore[method-assign]
            with patch.object(SessionOrchestrator, "_last_assistant_prose", return_value="more"):
                await orch._persist_status(out["id"], "done")            # a later turn-end: silent
                await orch._persist_status(out["id"], "in_progress")
                await orch._persist_status(out["id"], "done")            # still silent
                await _settle()
                assert sent == []
                await orch.archive_session(out["id"])
                await _settle()
            assert [s for s, _ in sent] == [parent.id]
            assert "<status>archived</status>" in sent[0][1]
        finally:
            await _stop_orch(orch)
    _run(run())


def test_notice_is_skipped_for_an_archived_parent(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'d.db'}", tmp_path=tmp_path,
                                max_active_sessions=20)
        try:
            parent = await orch.create_session(name="P", cwd=str(tmp_path), identity="u@x")
            out = await orch.dispatch_child_session(parent.id, name="child", prompt="x", model=None,
                                                    permission_mode=None, identity="u@x")
            orch._records[parent.id].status = "archived"
            orch.send_prompt = AsyncMock()                                 # type: ignore[method-assign]
            with patch.object(SessionOrchestrator, "_last_assistant_prose", return_value=""):
                await orch._persist_status(out["id"], "done")
                await _settle()
            orch.send_prompt.assert_not_awaited()
            assert orch._records[out["id"]].parent_notified_status == "done"
        finally:
            await _stop_orch(orch)
    _run(run())
