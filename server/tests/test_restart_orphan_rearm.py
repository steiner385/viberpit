"""Restart-orphaned asks hold the session; replays happen at most once (#2922).

* The rearm turn (``_rearm_session``) and the ``restore_deploy`` replay used to
  start a turn the moment the daemon came back. That turn cleared
  ``interrupted_by_restart`` and the re-offered card could no longer be answered.
  While an ask is orphaned both now wait, and the deferred rearm context rides
  along with the user's answer.
* A permission / plan orphan is answered through ``resolve_permission``: the
  decision is delivered as a prompt.
* ``restore_deploy`` and ``_rearm_session`` both replayed the interrupted prompt;
  now only one does. A system prompt (an orphaned answer, a resume nudge, an
  autopilot/completion nudge) is never recorded as the prompt to replay.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest
from sqlalchemy import select

from server.db import db_session, init_db
from server.db.models import Session as DbSession
from server.sessions import deploy_snapshot as ds
from server.sessions.engine import AgentEngine
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.orphaned_asks import RESTART_ASK_MESSAGE
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_factory import fake_sdk_factory

_SNAP = [{"tool_use_id": "toolu_mon", "kind": "monitor", "name": "Monitor",
          "input": {"description": "Watch PR", "command": "gh pr checks 7 --watch"},
          "description": "Watch PR"}]


class _D:
    """Stand-in driver recording what reaches it."""

    def __init__(self, turn_active=False):
        self.sent: list[str] = []
        self.turn_active = turn_active

    async def send_prompt(self, text, images=None, **_kw):
        self.sent.append(text)

    def resolve_permission(self, perm_id, decision, identity):
        return False

    def answer_question(self, qid, selected, identity, mode):
        return False


async def _orch_with_row(tmp_path, *, status, **row):
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'o.db'}")
    async with db_session() as db:
        db.add(DbSession(id="s1", name="n", account_name="a", cwd=str(tmp_path), **row))
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    await orch.rehydrate()
    rec = orch._records["s1"]
    rec.interrupted_by_restart = status is not None
    rec.interrupted_from_status = status
    driver = _D()

    async def _ensure(sid, identity="system"):
        orch._drivers[sid] = driver
        return driver

    orch.ensure_driver = _ensure  # type: ignore[method-assign]
    return orch, driver


async def _row(sid="s1"):
    async with db_session() as db:
        return (await db.execute(select(DbSession).where(DbSession.id == sid))).scalar_one()


# ── §3 hold the orphaned card open ───────────────────────────────────────────

def test_rearm_is_deferred_while_a_question_is_orphaned(tmp_path):
    async def run():
        orch, driver = await _orch_with_row(tmp_path, status="waiting_question",
                                            pending_rearm=_SNAP, pending_prompt="go on")
        assert await orch.rearm_background_tasks() == 0
        assert driver.sent == [], "a rearm turn would consume the orphaned card"
        assert "s1" not in orch._rearmed
        row = await _row()
        assert row.pending_rearm == _SNAP and row.pending_prompt == "go on"
        # The attach retry defers the same way.
        await orch._maybe_rearm_on_attach("s1")
        assert driver.sent == []

    asyncio.run(run())


def test_answering_the_orphaned_question_carries_the_deferred_rearm(tmp_path):
    async def run():
        orch, driver = await _orch_with_row(tmp_path, status="waiting_question",
                                            pending_rearm=_SNAP, pending_prompt="go on")
        await orch.rearm_background_tasks()
        assert await orch.answer_question("s1", "toolu_q-0", ["Ship it"], "tony") is True
        assert len(driver.sent) == 2
        answer, queued = driver.sent
        assert "My answer: Ship it" in answer
        assert "Watch PR" in answer and "relaunch the Monitor tool" in answer
        assert queued == "go on"
        assert "s1" in orch._rearmed
        row = await _row()
        assert row.pending_rearm is None and row.pending_prompt is None
        # The answer is not the user's prompt to replay after a later restart.
        assert "s1" not in orch._last_prompt

    asyncio.run(run())


def test_rearm_runs_normally_without_an_orphaned_ask(tmp_path):
    async def run():
        orch, driver = await _orch_with_row(tmp_path, status=None, pending_rearm=_SNAP)
        assert await orch.rearm_background_tasks() == 1
        assert len(driver.sent) == 1 and "Watch PR" in driver.sent[0]

    asyncio.run(run())


def test_rearm_rescans_and_drops_a_task_that_finished_during_the_drain(tmp_path):
    """A background command that completed between the drain snapshot and the
    restart must not be relaunched."""
    snap = [{"tool_use_id": "toolu_bg", "kind": "task", "name": "Bash",
             "input": {"command": "pytest -q", "run_in_background": True},
             "description": "Run tests"}]
    transcript = tmp_path / "t.jsonl"
    rows = [
        {"type": "assistant", "uuid": "u1", "message": {"content": [
            {"type": "tool_use", "id": "toolu_bg", "name": "Bash",
             "input": {"command": "pytest -q", "run_in_background": True}}]}},
        {"type": "user", "toolUseResult": {"backgroundTaskId": "b1"},
         "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_bg",
                                  "content": "Command running in background with ID: b1."}]}},
        {"type": "queue-operation", "operation": "enqueue", "content": (
            "<task-notification>\n<task-id>b1</task-id>\n<tool-use-id>toolu_bg"
            "</tool-use-id>\n<status>completed</status>\n</task-notification>")},
    ]
    transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    async def run():
        orch, driver = await _orch_with_row(tmp_path, status=None, pending_rearm=snap)
        orch.transcript_path = lambda sid: str(transcript)  # type: ignore[method-assign]
        await orch.rearm_background_tasks()
        assert driver.sent == [], "relaunched a command that had already finished"
        row = await _row()
        assert row.pending_rearm is None

    asyncio.run(run())


def test_rearm_of_a_finished_bash_reads_the_output_file(tmp_path):
    out = tmp_path / "b1.output"
    out.write_text("120 passed\n")
    snap = [{"tool_use_id": "toolu_bg", "kind": "task", "name": "Bash",
             "input": {"command": "pytest -q", "run_in_background": True},
             "description": "Run tests"}]
    transcript = tmp_path / "t.jsonl"
    rows = [
        {"type": "assistant", "uuid": "u1", "message": {"content": [
            {"type": "tool_use", "id": "toolu_bg", "name": "Bash",
             "input": {"command": "pytest -q", "run_in_background": True}}]}},
        {"type": "user", "toolUseResult": {"backgroundTaskId": "b1"},
         "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_bg",
                                  "content": ("Command running in background with ID: b1."
                                              f" Output is being written to: {out}.")}]}},
    ]
    transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    async def run():
        orch, driver = await _orch_with_row(tmp_path, status=None, pending_rearm=snap)
        orch.transcript_path = lambda sid: str(transcript)  # type: ignore[method-assign]
        await orch.rearm_background_tasks()
        [text] = driver.sent
        assert f"`{out}`" in text and "exists: yes" in text
        assert "read it first and relaunch only if it is incomplete" in text

    asyncio.run(run())


# ── §4 permission / plan orphans ─────────────────────────────────────────────

def _rec(**over):
    base = dict(id="s1", name="s", account="a", engine=AgentEngine.CLAUDE_CODE,
                engine_session_id="cs", cols=80, rows=24, cwd="/tmp", repo=None,
                branch=None, status="idle", name_is_auto=True,
                created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    base.update(over)
    return SessionRecord(**base)


def _bare_orch(rec, transcript=None):
    o = SessionOrchestrator.__new__(SessionOrchestrator)
    o._records = {rec.id: rec}
    o._drivers = {}
    o._last_prompt = {}
    o._rearmed = set()
    sent: list[tuple[str, dict]] = []

    async def _ensure(sid, identity=None):
        return object()

    async def _send(sid, text, attachment_ids=None, **kw):
        sent.append((text, kw))
        return "sent"

    async def _no_deferred(sid):
        return None, None

    o.ensure_driver = _ensure
    o.send_prompt = _send
    o._take_deferred_rearm = _no_deferred
    o.transcript_path = lambda sid: transcript
    return o, sent


def _transcript(tmp_path, name, inp):
    p = tmp_path / "p.jsonl"
    rows = [
        {"type": "assistant", "uuid": "u1", "message": {"content": [
            {"type": "tool_use", "id": "toolu_x", "name": name, "input": inp}]}},
        {"type": "user", "uuid": "r1", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_x", "is_error": True,
             "content": RESTART_ASK_MESSAGE}]}},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return str(p)


def test_an_orphaned_permission_allow_is_delivered_as_a_prompt(tmp_path):
    rec = _rec(interrupted_by_restart=True, interrupted_from_status="waiting_input")
    o, sent = _bare_orch(rec, _transcript(tmp_path, "Bash", {"command": "make"}))
    assert asyncio.run(o.resolve_permission("s1", "orphan:toolu_x", "allow", "tony")) is True
    [(text, kw)] = sent
    assert "Bash permission request" in text and "ALLOWED" in text
    assert "Re-run the tool call" in text
    assert kw.get("replayable") is False


def test_an_orphaned_plan_reject_is_delivered_as_a_prompt(tmp_path):
    rec = _rec(interrupted_by_restart=True, interrupted_from_status="ready_review")
    o, sent = _bare_orch(rec, _transcript(tmp_path, "ExitPlanMode", {"plan": "p"}))
    assert asyncio.run(o.resolve_permission("s1", "orphan:toolu_x", "deny", "tony")) is True
    assert "REJECTED" in sent[0][0]


def test_a_live_permission_still_wins():
    rec = _rec(interrupted_by_restart=True, interrupted_from_status="waiting_input")
    o, sent = _bare_orch(rec)

    class _Live(_D):
        def resolve_permission(self, perm_id, decision, identity):
            return True

    o._drivers = {"s1": _Live()}
    assert asyncio.run(o.resolve_permission("s1", "p1", "allow", "tony")) is True
    assert sent == []


def test_a_healthy_session_permission_miss_stays_false():
    o, sent = _bare_orch(_rec())
    assert asyncio.run(o.resolve_permission("s1", "p1", "allow", "tony")) is False
    assert sent == []


def test_an_orphaned_question_does_not_take_the_permission_fallback():
    rec = _rec(interrupted_by_restart=True, interrupted_from_status="waiting_question")
    o, sent = _bare_orch(rec)
    assert asyncio.run(o.resolve_permission("s1", "p1", "allow", "tony")) is False
    assert sent == []


# ── §6 replay at most once ───────────────────────────────────────────────────

class _Rec:
    def __init__(self, status, **kw):
        self.status = status
        self.host = None
        self.id = None
        self.interrupted_by_restart = kw.get("interrupted_by_restart", False)
        self.interrupted_from_status = kw.get("interrupted_from_status")


def _restore_orch(records, drivers=None):
    o = SessionOrchestrator.__new__(SessionOrchestrator)
    o._records = records
    o._drivers = {}
    o._resume_recovery = {}
    o._last_prompt = {}
    o._rearmed = set()
    built = drivers or {}

    async def _hosts():
        return None

    async def _liveness(ids):
        return set(ids), set()

    async def _ensure(sid, identity="system"):
        return built.setdefault(sid, _D())

    o._enabled_host_names = _hosts
    o._session_row_liveness = _liveness
    o.ensure_driver = _ensure
    return o, built


@pytest.mark.asyncio
async def test_restore_skips_a_prompt_rearm_already_replayed(tmp_path):
    p = str(tmp_path / "live.json")
    ds.write_snapshot([{"id": "s1", "status": "in_progress", "lastPrompt": "go on"}], path=p)
    o, built = _restore_orch({"s1": _Rec("idle")})
    # Rearm ran first at boot and replayed the same interrupted prompt.
    o._replayed = {"s1"}
    res = await o.restore_deploy(path=p)
    assert res["restored"] == ["s1"] and res["replayed"] == []
    assert built["s1"].sent == []


@pytest.mark.asyncio
async def test_restore_then_rearm_produces_exactly_one_replay(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'x.db'}"
    await init_db(db_url=db_url)
    async with db_session() as db:
        db.add(DbSession(id="s1", name="n", account_name="a", cwd=str(tmp_path),
                         pending_prompt="go on"))
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    await orch.rehydrate()
    driver = _D()

    async def _ensure(sid, identity="system"):
        orch._drivers[sid] = driver
        return driver

    orch.ensure_driver = _ensure  # type: ignore[method-assign]
    p = str(tmp_path / "live.json")
    ds.write_snapshot([{"id": "s1", "status": "in_progress", "lastPrompt": "go on"}], path=p)
    await orch.restore_deploy(path=p)
    await orch.rearm_background_tasks()
    assert driver.sent == ["go on"]


@pytest.mark.asyncio
async def test_restore_skips_the_replay_when_the_driver_is_mid_turn(tmp_path):
    p = str(tmp_path / "live.json")
    ds.write_snapshot([{"id": "s1", "status": "in_progress", "lastPrompt": "go on"}], path=p)
    o, built = _restore_orch({"s1": _Rec("idle")}, drivers={"s1": _D(turn_active=True)})
    res = await o.restore_deploy(path=p)
    assert res["replayed"] == [] and built["s1"].sent == []


@pytest.mark.asyncio
async def test_restore_skips_the_replay_while_an_ask_is_orphaned(tmp_path):
    p = str(tmp_path / "live.json")
    ds.write_snapshot([{"id": "s1", "status": "in_progress", "lastPrompt": "go on"}], path=p)
    o, built = _restore_orch({"s1": _Rec("idle", interrupted_by_restart=True,
                                         interrupted_from_status="waiting_question")})
    res = await o.restore_deploy(path=p)
    assert res["restored"] == ["s1"] and res["replayed"] == []
    assert built["s1"].sent == []
    assert "s1" not in o._last_prompt


def test_an_orphaned_answer_is_never_replayed_by_a_later_restore(tmp_path):
    """send_prompt(replayable=False) leaves nothing for snapshot_deploy to replay."""
    async def run():
        o = SessionOrchestrator.__new__(SessionOrchestrator)
        o._draining = False
        rec = _rec(status="in_progress")
        o._records = {"s1": rec}
        d = _D()
        o._drivers = {"s1": d}
        o._last_prompt = {"s1": "the user's earlier prompt"}

        async def _noop(*a, **k):
            return None

        o._retry_unapplied_model = _noop
        assert await o.send_prompt("s1", "[Answering the question…]", replayable=False) == "sent"
        assert d.sent == ["[Answering the question…]"]
        assert "s1" not in o._last_prompt, "a later restart would replay a stale prompt"
        snap = o.snapshot_deploy(path=str(tmp_path / "live.json"))
        assert all("lastPrompt" not in e for e in snap["live"])

    asyncio.run(run())


def test_system_origin_prompts_are_not_recorded_for_replay():
    async def run():
        o = SessionOrchestrator.__new__(SessionOrchestrator)
        o._draining = False
        o._records = {"s1": _rec(status="in_progress")}
        d = _D()
        o._drivers = {"s1": d}
        o._last_prompt = {}

        async def _noop(*a, **k):
            return None

        o._retry_unapplied_model = _noop
        await o.send_prompt("s1", "nudge", origin={"kind": "completion"})
        assert "s1" not in o._last_prompt
        await o.send_prompt("s1", "nudge", origin={"kind": "autopilot"})
        assert "s1" not in o._last_prompt
        await o.send_prompt("s1", "do the thing")
        assert o._last_prompt["s1"] == "do the thing"

    asyncio.run(run())


def test_resume_nudge_is_not_recorded_for_replay():
    async def run():
        rec = _rec(interrupted_by_restart=True, interrupted_from_status="in_progress")
        o, sent = _bare_orch(rec)
        assert await o.resume_interrupted("s1") == "resumed"
        assert sent[0][1].get("replayable") is False

    asyncio.run(run())


def test_create_driver_flags_a_resume_after_restart():
    from server.sessions.engine import AgentDriverConfig

    seen: list[AgentDriverConfig] = []

    class _Reg:
        def create(self, engine, config):
            seen.append(config)
            return object()

    o = SessionOrchestrator.__new__(SessionOrchestrator)
    o._engine_registry = _Reg()
    o._records = {"s1": _rec(interrupted_by_restart=True,
                             interrupted_from_status="in_progress")}
    kw = dict(engine=AgentEngine.CLAUDE_CODE, account="a", cwd="/tmp", config_dir="/c",
              mode="interactive", identity="me", on_sync_back=lambda: None)
    o._create_driver(session_id="s1", resume="cs", **kw)
    o._create_driver(session_id="s1", resume=None, **kw)
    o._records["s1"].interrupted_by_restart = False
    o._create_driver(session_id="s1", resume="cs", **kw)
    assert [c.resumed_after_restart for c in seen] == [True, False, False]
