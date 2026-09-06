"""Recurring-session scheduler dispatch (#603).

Drives single passes of ``_recurring_tick_once`` (extracted from the interval
loop) with an injected clock and a fake ``create_session``, so the cron-firing
logic and the persisted ``last_fired_at`` watermark are exercised with no
network and no real session machinery.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

RECURRING_ID = "recurring-nightly"
DAILY_8AM = {"cron": "0 8 * * *"}


def _build_orch():
    """A bare orchestrator with a fake create_session that records each spawn."""
    import server.sessions.orchestrator as orch_mod

    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    spawns: list[dict] = []

    async def _fake_create_session(**kwargs):
        spawns.append(kwargs)
        return type("Rec", (), {"id": f"sess-{len(spawns)}"})()

    orch.create_session = _fake_create_session  # type: ignore[method-assign]
    return orch, spawns


async def _init_db(tmp_path) -> None:
    from server.db import init_db
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'sched.db'}")


async def _seed(
    *, enabled: bool = True, schedule: dict | None = None,
    config: dict | None = None, last_fired_at: datetime | None = None,
) -> None:
    from server.db import db_session
    from server.db.models import Automation
    async with db_session() as db:
        db.add(Automation(
            id=RECURRING_ID, enabled=enabled,
            schedule=schedule if schedule is not None else DAILY_8AM,
            config=config if config is not None else {
                "recurringSession": {"name": "nightly", "prompt": "run nightly job",
                                     "repo": "owner/repo"}},
            last_fired_at=last_fired_at))


async def _row():
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import Automation
    async with db_session() as db:
        return (await db.execute(select(Automation).where(
            Automation.id == RECURRING_ID))).scalar_one()


def test_first_tick_establishes_watermark_without_spawning(tmp_path):
    async def run():
        await _init_db(tmp_path)
        orch, spawns = _build_orch()
        await _seed(last_fired_at=None)

        await orch._recurring_tick_once(now=datetime(2026, 7, 15, 7, 0))

        assert spawns == []  # never backfires on first sight
        assert (await _row()).last_fired_at == datetime(2026, 7, 15, 7, 0)

    asyncio.run(run())


def test_fires_a_session_when_cron_minute_arrives(tmp_path):
    async def run():
        await _init_db(tmp_path)
        orch, spawns = _build_orch()
        await _seed(last_fired_at=datetime(2026, 7, 15, 7, 59))

        await orch._recurring_tick_once(now=datetime(2026, 7, 15, 8, 0, 8))

        assert len(spawns) == 1
        kw = spawns[0]
        assert kw["initial_prompt"] == "run nightly job"
        assert kw["repo"] == "owner/repo"
        assert kw["origin"] == RECURRING_ID
        assert kw["origin_ref"] == "2026-07-15T08:00"
        assert (await _row()).last_fired_at == datetime(2026, 7, 15, 8, 0)

    asyncio.run(run())


def test_does_not_double_fire_on_a_second_tick_same_minute(tmp_path):
    async def run():
        await _init_db(tmp_path)
        orch, spawns = _build_orch()
        await _seed(last_fired_at=datetime(2026, 7, 15, 7, 59))

        await orch._recurring_tick_once(now=datetime(2026, 7, 15, 8, 0, 8))
        await orch._recurring_tick_once(now=datetime(2026, 7, 15, 8, 0, 40))

        assert len(spawns) == 1  # watermark suppresses the second firing

    asyncio.run(run())


def test_disabled_row_never_fires(tmp_path):
    async def run():
        await _init_db(tmp_path)
        orch, spawns = _build_orch()
        await _seed(enabled=False, last_fired_at=datetime(2026, 7, 15, 7, 59))

        await orch._recurring_tick_once(now=datetime(2026, 7, 15, 8, 0, 8))

        assert spawns == []

    asyncio.run(run())


def test_active_hours_gate_blocks_a_due_cron(tmp_path):
    async def run():
        await _init_db(tmp_path)
        orch, spawns = _build_orch()
        # Cron is due at 08:00 but the row's active-hours window is 09:00-18:00.
        await _seed(
            schedule={"cron": "0 8 * * *",
                      "activeHours": {"start": "09:00", "end": "18:00"}},
            last_fired_at=datetime(2026, 7, 15, 7, 59))

        await orch._recurring_tick_once(now=datetime(2026, 7, 15, 8, 0, 8))

        assert spawns == []  # gated out of hours

    asyncio.run(run())


def test_row_without_recurring_spec_is_ignored(tmp_path):
    async def run():
        await _init_db(tmp_path)
        orch, spawns = _build_orch()
        # A continuous automation may carry a cron/active-hours schedule but no
        # recurringSession spec — the dispatcher must leave it alone.
        await _seed(config={"someOtherKey": 1},
                    last_fired_at=datetime(2026, 7, 15, 7, 59))

        await orch._recurring_tick_once(now=datetime(2026, 7, 15, 8, 0, 8))

        assert spawns == []

    asyncio.run(run())


def test_catches_up_one_missed_run_after_restart(tmp_path):
    async def run():
        await _init_db(tmp_path)
        orch, spawns = _build_orch()
        # Last fired yesterday 08:00; daemon back up at 08:05 today → fire once.
        await _seed(last_fired_at=datetime(2026, 7, 14, 8, 0))

        await orch._recurring_tick_once(now=datetime(2026, 7, 15, 8, 5))

        assert len(spawns) == 1
        assert spawns[0]["origin_ref"] == "2026-07-15T08:00"

    asyncio.run(run())


def test_watermark_not_advanced_when_all_accounts_busy(tmp_path):
    async def run():
        from server.sessions.selection import AllAccountsBusy
        await _init_db(tmp_path)
        orch, spawns = _build_orch()

        async def _busy(**kwargs):
            raise AllAccountsBusy("no headroom")

        orch.create_session = _busy  # type: ignore[method-assign]
        await _seed(last_fired_at=datetime(2026, 7, 15, 7, 59))

        await orch._recurring_tick_once(now=datetime(2026, 7, 15, 8, 0, 8))

        # No headroom → watermark stays put so the firing retries next tick.
        assert (await _row()).last_fired_at == datetime(2026, 7, 15, 7, 59)

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Engine / reasoning-effort preservation (#1270)
# --------------------------------------------------------------------------- #


def test_engine_naming_what_it_actually_resolves_to_still_fires(tmp_path):
    """A recurring spec that names its own (correctly unaudited) engine is not
    penalized for saying so out loud — it fires exactly as an unlabeled spec
    would."""

    async def run():
        await _init_db(tmp_path)
        orch, spawns = _build_orch()
        await _seed(last_fired_at=datetime(2026, 7, 15, 7, 59), config={
            "recurringSession": {"name": "nightly", "prompt": "run nightly job",
                                 "engine": "claude_code"}})

        await orch._recurring_tick_once(now=datetime(2026, 7, 15, 8, 0, 8))

        assert len(spawns) == 1

    asyncio.run(run())


def test_engine_forwarded_and_reasoning_effort_preserved_for_an_audited_origin(
        tmp_path, monkeypatch):
    """When this origin's own audited ceiling resolves to Codex, a recurring
    spec's ``reasoningEffort`` reaches ``create_session`` through the ordinary
    contract — no parallel Codex-only path. ``AutomationContext.spawn_session``
    (already covered end to end by ``test_automation_engine_policy_spawn.py``)
    is faked here so this test isolates the new gate in ``_recurring_spawn``
    itself rather than re-exercising the sanitize/permission-mode machinery."""

    async def run():
        from ccswitch.engines import AgentEngine
        from server.automations.context import AutomationContext

        await _init_db(tmp_path)
        orch, spawns = _build_orch()
        spawn_calls: list[dict] = []

        async def _fake_launch_plan(self):
            return AgentEngine.CODEX, {}

        async def _fake_spawn_session(self, *, origin_ref, dedupe_key=None,
                                      detail=None, **kwargs):
            spawn_calls.append(kwargs)
            return type("Rec", (), {"id": "sess-1"})()

        monkeypatch.setattr(AutomationContext, "launch_plan", _fake_launch_plan)
        monkeypatch.setattr(AutomationContext, "spawn_session", _fake_spawn_session)
        await _seed(last_fired_at=datetime(2026, 7, 15, 7, 59), config={
            "recurringSession": {"name": "nightly", "prompt": "run nightly job",
                                 "engine": "codex", "reasoningEffort": "medium"}})

        await orch._recurring_tick_once(now=datetime(2026, 7, 15, 8, 0, 8))

        assert len(spawn_calls) == 1
        assert spawn_calls[0]["reasoning_effort"] == "medium"
        assert spawns == []  # create_session bypassed by the faked spawn_session
        assert (await _row()).last_fired_at == datetime(2026, 7, 15, 8, 0)

    asyncio.run(run())


def test_engine_mismatch_is_refused_visibly_not_run_as_claude(tmp_path):
    """A spec naming an engine this (unaudited, ad-hoc) origin cannot run must
    be refused loudly, not silently downgraded to Claude."""

    async def run():
        from server.automations.context import AutomationContext

        await _init_db(tmp_path)
        orch, spawns = _build_orch()
        await _seed(last_fired_at=datetime(2026, 7, 15, 7, 59), config={
            "recurringSession": {"name": "nightly", "prompt": "run nightly job",
                                 "engine": "codex"}})

        await orch._recurring_tick_once(now=datetime(2026, 7, 15, 8, 0, 8))

        assert spawns == []
        # A persistent misconfiguration is not retriable — the watermark still
        # advances so it doesn't retry-storm every tick.
        assert (await _row()).last_fired_at == datetime(2026, 7, 15, 8, 0)
        events = await AutomationContext(RECURRING_ID).list_events()
        assert any(e["kind"] == "engine_policy_refused" for e in events)

    asyncio.run(run())


def test_unknown_engine_name_is_refused_visibly(tmp_path):
    async def run():
        await _init_db(tmp_path)
        orch, spawns = _build_orch()
        await _seed(last_fired_at=datetime(2026, 7, 15, 7, 59), config={
            "recurringSession": {"name": "nightly", "prompt": "run nightly job",
                                 "engine": "gpt-9"}})

        await orch._recurring_tick_once(now=datetime(2026, 7, 15, 8, 0, 8))

        assert spawns == []
        assert (await _row()).last_fired_at == datetime(2026, 7, 15, 8, 0)

    asyncio.run(run())


def test_reasoning_effort_without_codex_is_refused_visibly(tmp_path):
    """``reasoningEffort`` only means something for Codex; asking for it on an
    origin that resolves to Claude must refuse rather than pass the kwarg
    through to a driver that has no use for it."""

    async def run():
        from server.automations.context import AutomationContext

        await _init_db(tmp_path)
        orch, spawns = _build_orch()
        await _seed(last_fired_at=datetime(2026, 7, 15, 7, 59), config={
            "recurringSession": {"name": "nightly", "prompt": "run nightly job",
                                 "reasoningEffort": "medium"}})

        await orch._recurring_tick_once(now=datetime(2026, 7, 15, 8, 0, 8))

        assert spawns == []
        assert (await _row()).last_fired_at == datetime(2026, 7, 15, 8, 0)
        events = await AutomationContext(RECURRING_ID).list_events()
        assert any(e["kind"] == "engine_policy_refused" for e in events)

    asyncio.run(run())
