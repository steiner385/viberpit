"""SdlcIntakeLoop — the propose-only SDLC Intake control loop.

Every I/O dependency is INJECTED so the loop runs with no network / no real
sessions. These tests drive the loop entirely through fakes and assert on the
recorded spawns / artifact writes / created initiatives and the activity feed.
The loop itself only *decides* by delegating to the pure ``sdlc.engine`` and
seeds the intent session with the pure ``sdlc.prompts`` builder — mirroring how
``BacklogRunnerLoop`` drives its pure cores.
"""
from __future__ import annotations

import asyncio

from server.automations.context import AutomationContext
from server.db import db_session, init_db
from server.sdlc.engine import ArtifactView, InitiativeView


class FakeClock:
    """A monotonic clock the tests advance by hand."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make_config(**over) -> dict:
    cfg = {"enabled_repos": [], "max_spawns_per_day": 10}
    cfg.update(over)
    return cfg


async def _seed_row(automation_id: str = "sdlc-intake") -> None:
    from server.db.models import Automation
    async with db_session() as db:
        db.add(Automation(id=automation_id))


class _Sess:
    def __init__(self, sid: str) -> None:
        self.id = sid


def build_loop(*, mode="propose", config=None, clock=None,
               triggers=None, existing_artifacts=None, spawn=None):
    """Construct an SdlcIntakeLoop over recorder fakes.

    Returns (loop, recorders, ctx) where recorders exposes .spawns / .records /
    .initiatives / .artifacts so tests assert on what the loop did.
    """
    from server.automations.sdlc_intake import SdlcIntakeLoop

    cfg = config if config is not None else make_config()
    clk = clock or FakeClock()
    ctx = AutomationContext("sdlc-intake")

    class Recorders:
        def __init__(self):
            self.spawns: list = []       # (Trigger, prompt)
            self.records: list = []      # (slug, repo_path, session_id)
            self.initiatives: dict = {}  # slug -> InitiativeView
            self.artifacts: dict = dict(existing_artifacts or {})  # slug -> [ArtifactView]

    rec = Recorders()

    async def read_config():
        return mode, cfg

    async def read_triggers():
        return list(triggers or [])

    async def ensure_initiative(trigger):
        view = rec.initiatives.get(trigger.slug)
        if view is None:
            view = InitiativeView(slug=trigger.slug, stage="intake",
                                  status="active", repo=trigger.repo)
            rec.initiatives[trigger.slug] = view
        return view, list(rec.artifacts.get(trigger.slug, []))

    async def record_intent(slug, repo_path, session_id):
        rec.records.append((slug, repo_path, session_id))
        rec.artifacts.setdefault(slug, []).append(
            ArtifactView(kind="intent", commit_sha=None))

    async def _default_spawn(trigger, prompt):
        rec.spawns.append((trigger, prompt))
        return _Sess(f"sess-{trigger.slug}")

    loop = SdlcIntakeLoop(
        ctx,
        read_config=read_config,
        read_triggers=read_triggers,
        ensure_initiative=ensure_initiative,
        record_intent=record_intent,
        spawn=spawn or _default_spawn,
        clock=clk,
    )
    return loop, rec, ctx


def trig(slug: str, text: str = "we want a thing", repo: str = "steiner385/x"):
    from server.automations.sdlc_intake import Trigger
    return Trigger(slug=slug, text=text, repo=repo)


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #

def test_propose_mode_spawns_intent_and_records_artifact(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'a.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, ctx = build_loop(
            triggers=[trig("dark-mode", text="users want dark mode")])
        await loop.tick()

        # one intent session spawned, seeded with the pure intent prompt
        assert len(rec.spawns) == 1
        t, prompt = rec.spawns[0]
        assert t.slug == "dark-mode"
        assert "users want dark mode" in prompt
        assert "docs/sdlc/dark-mode/intent.md" in prompt

        # the initiative was created at the intake stage
        assert "dark-mode" in rec.initiatives
        assert rec.initiatives["dark-mode"].stage == "intake"

        # the intent artifact row was recorded, linked to the spawned session
        assert rec.records == [("dark-mode", "docs/sdlc/dark-mode/intent.md", "sess-dark-mode")]

        # a proposed event was emitted
        proposed = await ctx.list_events(kind="proposed")
        assert len(proposed) == 1

    asyncio.run(run())


def test_off_mode_does_nothing(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'b.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, _ = build_loop(mode="off", triggers=[trig("x")])
        await loop.tick()
        assert rec.spawns == []
        assert rec.initiatives == {}
        assert rec.records == []

    asyncio.run(run())


def test_existing_intent_awaits_approval_no_respawn(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'c.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        # an intent artifact already exists (PR open, commit_sha None) → the pure
        # engine returns AwaitApproval; the loop must not spawn a second session.
        loop, rec, _ = build_loop(
            triggers=[trig("dark-mode")],
            existing_artifacts={"dark-mode": [ArtifactView(kind="intent", commit_sha=None)]})
        await loop.tick()
        assert rec.spawns == []
        assert rec.records == []

    asyncio.run(run())


def test_daily_cap_limits_spawns_per_tick(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'd.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, _ = build_loop(
            config=make_config(max_spawns_per_day=1),
            triggers=[trig("a"), trig("b")])
        await loop.tick()
        assert len(rec.spawns) == 1  # capped

    asyncio.run(run())


def test_same_trigger_across_ticks_spawns_once(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'e.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed_row()
        loop, rec, _ = build_loop(triggers=[trig("dark-mode")])
        await loop.tick()   # proposes intent, records the artifact
        await loop.tick()   # artifact now exists → AwaitApproval, no respawn
        assert len(rec.spawns) == 1
        assert len(rec.records) == 1

    asyncio.run(run())


def test_automation_registered_with_expected_identity():
    from server.automations.sdlc_intake import SdlcIntake
    a = SdlcIntake()
    assert a.id == "sdlc-intake"
    assert a.modes[0] == "off"
    assert "propose" in a.modes

    from server.automations.registry import registry
    assert registry.get("sdlc-intake") is not None
