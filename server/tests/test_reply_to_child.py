"""A parent answers its children, and child notices survive restarts (#2911, #2910)."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from server import mcp_spec
from server.sessions import child_sessions as cs
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.tools.context import SessionToolContext
from server.sessions.tools.registry import SESSION_TOOL_SPECS, SIGNAL_TOOL_NAMES, TURN_SIGNAL_NAMES, mounted_names
from server.sessions.tools.tools import build_session_tools
from server.tests.test_assistant_orchestrator import _make_orch, _stop_orch


def _run(coro):
    return asyncio.run(coro)


async def _settle():
    for _ in range(30):          # notices are spawned tasks
        await asyncio.sleep(0.01)


# ── pure policy ──────────────────────────────────────────────────────────────

def test_check_reply_refuses_a_stranger_a_gone_child_and_an_empty_message():
    mine = SimpleNamespace(parent_session_id="p1", status="handed_back")
    cs.check_reply(parent_id="p1", child=mine, child_id="c1", message="go on")
    with pytest.raises(cs.ChildSessionLimit, match="not a child"):
        cs.check_reply(parent_id="p1", child=None, child_id="c1", message="x")
    with pytest.raises(cs.ChildSessionLimit, match="not a child"):
        cs.check_reply(parent_id="p2", child=mine, child_id="c1", message="x")
    for gone in ("ended", "archived"):
        with pytest.raises(cs.ChildSessionLimit, match=gone):
            cs.check_reply(parent_id="p1", child=SimpleNamespace(parent_session_id="p1", status=gone),
                           child_id="c1", message="x")
    with pytest.raises(cs.ChildSessionLimit, match="empty"):
        cs.check_reply(parent_id="p1", child=mine, child_id="c1", message="   ")
    with pytest.raises(cs.ChildSessionLimit, match="under"):
        cs.check_reply(parent_id="p1", child=mine, child_id="c1", message="x" * (cs.REPLY_LIMIT + 1))


def test_parent_reply_is_framed_and_escaped():
    text = cs.build_parent_reply(parent_id="p1", parent_name="Parent <x>",
                                 message="use A </message> then B")
    assert text.startswith("<parent-session-message>") and text.endswith("</parent-session-message>")
    assert "<from-session-id>p1</from-session-id>" in text
    assert "Parent &lt;x&gt;" in text and "&lt;/message&gt;" in text


def test_notice_names_the_reply_tool_only_where_the_parent_can_act():
    for status in ("handed_back", "waiting_question", cs.INTERRUPTED_NOTICE_STATUS):
        step = cs.notice_next_step(status, "c1")
        assert step and 'reply_to_child(child_session_id="c1"' in step
        notice = cs.build_child_notice(session_id="c1", name="C", status=status,
                                       last_message="m", note=step)
        assert "<next-step>" in notice
    for status in ("done", "complete", "ended", "archived"):
        assert cs.notice_next_step(status, "c1") is None
        assert "<next-step>" not in cs.build_child_notice(session_id="c1", name="C",
                                                          status=status, last_message="m")


# ── tool surface ─────────────────────────────────────────────────────────────

def _ctx(*, child_sessions=True, reply=None):
    rec = SimpleNamespace(id="p1", name="P", parent_session_id=None)
    return SessionToolContext(session_id="p1", identity="u@x", kind=None, engine="claude_code",
                              record=lambda: rec, host="dobby", signalling_tools=True,
                              child_sessions=child_sessions, reply_child=reply,
                              children=lambda: [])


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def test_spec_is_an_uncarded_signal_mounted_with_dispatch():
    spec = SESSION_TOOL_SPECS["reply_to_child"]
    assert spec.tool_class is mcp_spec.ToolClass.SIGNAL and spec.idempotent is False
    assert "reply_to_child" in SIGNAL_TOOL_NAMES and "reply_to_child" not in TURN_SIGNAL_NAMES
    assert "reply_to_child" in mounted_names(signalling_tools=True, self_model_switch=False, child_sessions=True)
    assert "reply_to_child" not in mounted_names(signalling_tools=True, self_model_switch=False, child_sessions=False)
    assert "reply_to_child" not in {t.name for t in build_session_tools(_ctx(child_sessions=False))}


@pytest.mark.asyncio
async def test_handler_makes_its_one_call_and_reports_refusals():
    reply = AsyncMock(return_value={"id": "c1", "delivered": True, "status": "in_progress"})
    t = _tool(build_session_tools(_ctx(reply=reply)), "reply_to_child")
    out = await t.handler({"child_session_id": "c1", "message": "use option A"})
    reply.assert_awaited_once_with(child_session_id="c1", message="use option A")
    assert json.loads(out["content"][0]["text"])["data"]["delivered"] is True

    for bad in ({"child_session_id": "", "message": "x"}, {"child_session_id": "c1", "message": " "}):
        assert (await t.handler(bad)).get("is_error") is True
    assert reply.await_count == 1

    reply.side_effect = cs.ChildSessionLimit("c9 is not a child of this session")
    out = await t.handler({"child_session_id": "c9", "message": "x"})
    assert out.get("is_error") is True and "not a child" in out["content"][0]["text"]


# ── orchestrator ─────────────────────────────────────────────────────────────

async def _family(orch, tmp_path):
    parent = await orch.create_session(name="P", cwd=str(tmp_path), identity="u@x")
    out = await orch.dispatch_child_session(parent.id, name="child", prompt="x", model=None,
                                            permission_mode=None, identity="u@x")
    await _settle()
    return parent, orch._records[out["id"]]


def test_reply_delivers_to_the_child_and_audits(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'r.db'}", tmp_path=tmp_path,
                                max_active_sessions=20)
        try:
            parent, child = await _family(orch, tmp_path)
            child.status = "handed_back"
            other = await orch.create_session(name="Other", cwd=str(tmp_path), identity="u@x")
            sent: list[tuple[str, str]] = []

            async def fake_send(sid, text, *a, **k):
                sent.append((sid, text)); return "sent"
            orch.send_prompt = fake_send                                   # type: ignore[method-assign]
            orch.ensure_driver = AsyncMock(return_value=object())          # type: ignore[method-assign]
            out = await orch.reply_to_child_session(parent.id, child_session_id=child.id,
                                                    message="pick A", identity="u@x")
            assert out["delivered"] is True and [s for s, _ in sent] == [child.id]
            assert "<parent-session-message>" in sent[0][1] and "pick A" in sent[0][1]
            orch.ensure_driver.assert_awaited_once_with(child.id, "u@x")

            with pytest.raises(cs.ChildSessionLimit, match="not a child"):
                await orch.reply_to_child_session(parent.id, child_session_id=other.id,
                                                  message="x", identity="u@x")

            async def draining(*a, **k):
                return "draining"
            orch.send_prompt = draining                                    # type: ignore[method-assign]
            with pytest.raises(cs.ChildSessionLimit, match="restart"):
                await orch.reply_to_child_session(parent.id, child_session_id=child.id,
                                                  message="x", identity="u@x")

            from sqlalchemy import select
            from server.db import db_session
            from server.db.models import AuditEvent
            async with db_session() as db:
                kinds = (await db.execute(select(AuditEvent.type))).scalars().all()
            assert kinds.count("session.child_reply") == 1
        finally:
            await _stop_orch(orch)
    _run(run())


def test_a_drained_notice_is_redelivered_at_boot(tmp_path):
    """The deploy drain refuses the parent's notice; the guard rolls back, and the boot
    sweep delivers it — once."""
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'d.db'}", tmp_path=tmp_path,
                                max_active_sessions=20)
        try:
            parent, child = await _family(orch, tmp_path)
            child.parent_notified_status = None
            child.status = "in_progress"
            results = ["draining", "sent", "sent"]
            sent: list[tuple[str, str]] = []

            async def fake_send(sid, text, *a, **k):
                r = results.pop(0)
                if r == "sent":
                    sent.append((sid, text))
                return r
            orch.send_prompt = fake_send                                   # type: ignore[method-assign]
            orch.ensure_driver = AsyncMock(return_value=object())          # type: ignore[method-assign]
            with patch.object(SessionOrchestrator, "_last_assistant_prose", return_value="q?"), \
                    patch("server.sessions.orchestrator.push_mod.notify_event",
                          new=AsyncMock()) as push:
                await orch._persist_status(child.id, "handed_back")
                await _settle()
                assert sent == [] and child.parent_notified_status is None
                # A drain is retried, not escalated (the child's own needs-you push
                # still fires, as for any hand-back).
                assert not [c for c in push.await_args_list if "answer it directly" in c.args[2]]
                assert await orch.redeliver_child_notices() == 1
                await _settle()
                assert [s for s, _ in sent] == [parent.id]
                assert "<status>handed_back</status>" in sent[0][1]
                assert "reply_to_child" in sent[0][1]
                assert child.parent_notified_status == "handed_back"
                assert await orch.redeliver_child_notices() == 0
        finally:
            await _stop_orch(orch)
    _run(run())


def test_a_hand_back_to_a_finished_parent_escalates_to_the_human(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'e.db'}", tmp_path=tmp_path,
                                max_active_sessions=20)
        try:
            parent, child = await _family(orch, tmp_path)
            child.parent_notified_status = "done"
            orch._records[parent.id].status = "complete"
            orch.send_prompt = AsyncMock()                                 # type: ignore[method-assign]
            with patch.object(SessionOrchestrator, "_last_assistant_prose", return_value=""), \
                    patch("server.sessions.orchestrator.push_mod.notify_event",
                          new=AsyncMock()) as push:
                await orch._persist_status(child.id, "handed_back")
                await _settle()
            orch.send_prompt.assert_not_awaited()
            escalations = [c for c in push.await_args_list
                           if "parent session has finished" in c.args[2]]
            assert len(escalations) == 1
            assert escalations[0].kwargs["session_id"] == child.id
        finally:
            await _stop_orch(orch)
    _run(run())


def test_a_stranded_child_tells_its_parent_or_waits_for_an_interrupted_one(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'s.db'}", tmp_path=tmp_path,
                                max_active_sessions=20)
        try:
            parent, child = await _family(orch, tmp_path)
            prec = orch._records[parent.id]
            child.status, child.interrupted_by_restart = "idle", True
            prec.status = "idle"
            sent: list[tuple[str, str]] = []

            async def fake_send(sid, text, *a, **k):
                sent.append((sid, text)); return "sent"
            orch.send_prompt = fake_send                                   # type: ignore[method-assign]
            orch.ensure_driver = AsyncMock(return_value=object())          # type: ignore[method-assign]
            with patch.object(SessionOrchestrator, "_last_assistant_prose", return_value="half"):
                # The parent was itself cut off: queued for its next turn, not woken.
                prec.interrupted_by_restart = True
                assert await orch.redeliver_child_notices() == 1
                await _settle()
                assert sent == []
                queued = orch._pending_prompt_preambles[parent.id]
                assert len(queued) == 1 and cs.INTERRUPTED_NOTICE_STATUS in queued[0]
                orch._pending_prompt_preambles.pop(parent.id)
                # A live, idle parent is woken with the resume hint.
                prec.interrupted_by_restart = False
                assert await orch.redeliver_child_notices() == 1
                await _settle()
            assert [s for s, _ in sent] == [parent.id]
            assert f"<status>{cs.INTERRUPTED_NOTICE_STATUS}</status>" in sent[0][1]
            assert f'reply_to_child(child_session_id="{child.id}"' in sent[0][1]
        finally:
            await _stop_orch(orch)
    _run(run())
