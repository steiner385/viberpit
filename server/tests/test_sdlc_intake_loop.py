"""SdlcIntakeLoop (#799): injected-I/O control loop, no network/DB."""
from types import SimpleNamespace

import pytest

from server.automations.context import AutomationContext
from server.automations.sdlc_intake import SdlcIntake, SdlcIntakeLoop


class Recorder:
    def __init__(self, initiatives, mode="propose"):
        self.initiatives = initiatives
        self.mode = mode
        self.artifacts: dict[str, list[dict]] = {}
        self.recorded: list[dict] = []
        self.statuses: list[tuple[str, str]] = []
        self.spawned: list[tuple[str, str]] = []
        self.spawn_result = SimpleNamespace(id="sess-1")

    async def list_initiatives(self):
        return self.initiatives

    async def list_artifacts(self, iid):
        return self.artifacts.get(iid, [])

    async def record_artifact(self, **kw):
        self.recorded.append(kw)

    async def set_status(self, iid, status):
        self.statuses.append((iid, status))

    async def spawn(self, ini, kind):
        self.spawned.append((ini["slug"], kind))
        return self.spawn_result

    async def read_config(self):
        return self.mode, {}


def _loop(rec):
    ctx = AutomationContext("sdlc-intake")  # emit() best-effort no-ops without DB
    return SdlcIntakeLoop(
        ctx, list_initiatives=rec.list_initiatives, list_artifacts=rec.list_artifacts,
        record_artifact=rec.record_artifact, set_status=rec.set_status,
        spawn=rec.spawn, read_config=rec.read_config, clock=lambda: 0.0)


INTAKE = {"id": "i1", "slug": "dark-mode", "repo": "o/r", "title": "Dark mode",
          "stage": "intake", "status": "proposed", "origin": "human",
          "origin_ref": None}


@pytest.mark.asyncio
async def test_proposes_intent_for_bare_intake_initiative():
    rec = Recorder([dict(INTAKE)])
    await _loop(rec).tick()
    assert rec.spawned == [("dark-mode", "intent")]
    assert rec.recorded == [{
        "initiative_id": "i1", "kind": "intent",
        "repo_path": "docs/sdlc/dark-mode/intent.md",
        "session_id": "sess-1", "model": None}]
    assert rec.statuses == [("i1", "awaiting_human")]


@pytest.mark.asyncio
async def test_off_mode_does_nothing():
    rec = Recorder([dict(INTAKE)], mode="off")
    await _loop(rec).tick()
    assert rec.spawned == [] and rec.recorded == [] and rec.statuses == []


@pytest.mark.asyncio
async def test_existing_intent_artifact_is_not_reproposed():
    rec = Recorder([dict(INTAKE)])
    rec.artifacts["i1"] = [{"id": "a1", "kind": "intent", "pr_number": None,
                            "pr_url": None, "commit_sha": None, "session_id": "s0"}]
    await _loop(rec).tick()
    assert rec.spawned == []


@pytest.mark.asyncio
async def test_non_intake_stages_are_ignored():
    rec = Recorder([dict(INTAKE, stage="design")])
    await _loop(rec).tick()
    assert rec.spawned == []


@pytest.mark.asyncio
async def test_failed_spawn_records_nothing():
    rec = Recorder([dict(INTAKE)])
    rec.spawn_result = None
    await _loop(rec).tick()
    assert rec.recorded == [] and rec.statuses == []


def test_automation_contract():
    a = SdlcIntake()
    assert a.id == "sdlc-intake"
    assert a.modes == ("off", "propose", "auto")
    assert a.status(ctx=None).level in ("idle", "active", "warn", "error")
