"""Systemic-red wiring: the orchestrator builds SystemicRedLoop on the doctor's ctx,
starts it as ``cicd-systemic-loop``, reaps it on stop, and the card exposes its
actions (spec 2026-10-31 §5, §8)."""
from __future__ import annotations

import asyncio
import inspect
import time
from types import SimpleNamespace

import pytest

from server.db import db_session, dispose_db, init_db
from server.tests.fakes.fake_factory import fake_sdk_factory


def _new_orch():
    from server.sessions.orchestrator import SessionOrchestrator
    return SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)


@pytest.mark.asyncio
async def test_orchestrator_wires_systemic_loop_on_cicd_ctx(tmp_path):
    from server.automations.cicd.systemic_loop import SystemicRedLoop
    await init_db(f"sqlite+aiosqlite:///{tmp_path / 'c.db'}")
    try:
        orch = _new_orch()
        loop = orch._cicd_systemic_loop
        assert isinstance(loop, SystemicRedLoop)
        assert loop._ctx is orch._cicd_ctx
        assert loop._read_config == orch._cicd_read_config
        assert loop._list_repos == orch._cicd_list_repos
        # The ctx can spawn: fix sessions need a bound create_session.
        assert orch._cicd_ctx._create_session is not None
        # An empty alarm prefix reads nothing (no aws CLI call).
        assert await loop._alarms({"aws_alarm_prefix": "", "aws_profile": ""}) == []
    finally:
        await dispose_db()


def test_systemic_loop_is_primary_only_and_started_beside_the_doctor():
    from server.sessions import orchestrator as mod
    assert "_cicd_systemic_task" in mod._PRIMARY_ONLY_LOOP_ATTRS
    start_src = inspect.getsource(mod.SessionOrchestrator.start)
    assert 'name="cicd-systemic-loop"' in start_src
    assert "_cicd_systemic_tick_loop(interval=120.0)" in start_src
    stop_src = inspect.getsource(mod.SessionOrchestrator.stop)
    assert '"_cicd_systemic_task"' in stop_src


@pytest.mark.asyncio
async def test_systemic_task_is_reaped_on_shutdown(tmp_path, monkeypatch):
    monkeypatch.delenv("VIBERPIT_DAEMON_ROLE", raising=False)
    await init_db(f"sqlite+aiosqlite:///{tmp_path / 'c.db'}")
    try:
        orch = _new_orch()
        await orch.start()
        task = orch._cicd_systemic_task
        assert task is not None and task.get_name() == "cicd-systemic-loop"
        await orch.stop()
        assert task.done() and orch._cicd_systemic_task is None
    finally:
        await dispose_db()


@pytest.mark.asyncio
async def test_systemic_tick_respects_the_doctor_schedule():
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    ticks: list[str] = []

    async def allows(automation_id):
        ticks.append(automation_id)
        return False

    orch._schedule_allows_tick = allows

    async def boom():
        raise AssertionError("tick must be gated by the schedule")

    orch._cicd_systemic_loop = SimpleNamespace(tick=boom)
    await SessionOrchestrator._cicd_systemic_tick_once(orch)
    assert ticks == ["cicd-doctor"]


@pytest.mark.asyncio
async def test_failed_hourly_counts_aggregate_merge_group_failures(tmp_path):
    from server.db.models import CiRun
    from server.sessions.orchestrator import SessionOrchestrator
    await init_db(f"sqlite+aiosqlite:///{tmp_path / 'c.db'}")
    try:
        now = time.time()
        rows = [
            # last hour: 3 failed + 1 success merge_group, 1 failed push (ignored)
            (1, "merge_group", "failure", now - 60), (2, "merge_group", "failure", now - 600),
            (3, "merge_group", "failure", now - 1800), (4, "merge_group", "success", now - 900),
            (5, "push", "failure", now - 300),
            # earlier: one failure 5 h ago, one 15 days ago (outside the 14-day window)
            (6, "merge_group", "failure", now - 5 * 3600 - 10),
            (7, "merge_group", "failure", now - 15 * 86400),
            # another repo
            (8, "merge_group", "failure", now - 60),
        ]
        async with db_session() as db:
            for rid, event, concl, ts in rows:
                db.add(CiRun(id=rid, repo="o/other" if rid == 8 else "o/r", event=event,
                             conclusion=concl, created_ts=ts))
            await db.flush()
        orch = SessionOrchestrator.__new__(SessionOrchestrator)
        orch._clock = lambda: now
        failed, hourly, ratio, runs = await SessionOrchestrator._cicd_systemic_hourly_counts(
            orch, "o/r")
        assert (failed, runs) == (3, 4) and ratio == pytest.approx(0.75)
        assert len(hourly) == 14 * 24 and sum(hourly) == 1 and hourly[4] == 1
    finally:
        await dispose_db()


def test_card_provider_offers_systemic_actions_and_maps_statuses():
    from server.automations.action_providers import CicdDoctorActionProvider
    from server.automations.base import ActionableItem, ActionSpec
    calls: list[tuple[str, str]] = []

    class Loop:
        async def actionable_items(self):
            return [ActionableItem(id="systemic:o/r:abc", title="t",
                                   actions=[ActionSpec(kind="clear_brake", label="Clear brake")])]

        async def action_result(self, key, kind):
            calls.append((key, kind))
            return {"clear_brake": "brake_cleared", "dismiss": "dismissed"}[kind]

    async def findings():
        return [{"id": "f1", "repo": "o/r", "title": "flaky"}]

    async def file_now(fid):
        calls.append((fid, "file_issue"))
        return "filed"

    orch = SimpleNamespace(_cicd_systemic_loop=Loop(), cicd_actionable_findings=findings,
                           cicd_file_issue_now=file_now)
    p = CicdDoctorActionProvider(orch)
    assert p.valid_kinds == frozenset({"file_issue", "clear_brake", "dismiss"})
    ids = [i.id for i in asyncio.run(p.actionable())]
    assert ids == ["systemic:o/r:abc", "f1"]
    r = asyncio.run(p.trigger("systemic:o/r:abc", "clear_brake"))
    assert r.status == "brake_cleared" and r.level == "ok"
    r = asyncio.run(p.trigger("systemic:o/r:abc", "dismiss"))
    assert r.status == "dismissed" and r.level == "ok"
    r = asyncio.run(p.trigger("f1", "file_issue"))
    assert r.status == "filed"
    assert calls == [("systemic:o/r:abc", "clear_brake"), ("systemic:o/r:abc", "dismiss"),
                     ("f1", "file_issue")]


def test_web_notification_labels_include_ci_classes():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    src = (root / "web/src/features/settings/sections/notificationsApi.ts").read_text()
    assert "{ value: 'ci_systemic_red', label: 'CI systemic red' }" in src
    assert "{ value: 'ci_spend_alarm', label: 'CI spend alarm' }" in src


@pytest.mark.asyncio
async def test_read_config_null_mode_is_propose_and_correct_only_when_explicit(tmp_path):
    from server.db.models import Automation
    from server.sessions.orchestrator import SessionOrchestrator
    await init_db(f"sqlite+aiosqlite:///{tmp_path / 'c.db'}")
    try:
        orch = SessionOrchestrator.__new__(SessionOrchestrator)
        async with db_session() as db:
            db.add(Automation(id="cicd-doctor", enabled=True, mode=None,
                              config={"systemic_brake": False}))
            await db.flush()
        mode, config = await SessionOrchestrator._cicd_read_config(orch)
        assert mode == "propose" and config["systemic_brake"] is False
        async with db_session() as db:
            row = await db.get(Automation, "cicd-doctor")
            row.mode = "correct"
            await db.flush()
        mode, _ = await SessionOrchestrator._cicd_read_config(orch)
        assert mode == "correct"
    finally:
        await dispose_db()
