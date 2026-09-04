"""SdlcSynthesizerLoop (#800): design→spec, plan→plan proposals + PR enrichment."""
from types import SimpleNamespace

import pytest

from server.automations.context import AutomationContext
from server.automations.sdlc_synthesizer import SdlcSynthesizer, SdlcSynthesizerLoop


class Recorder:
    def __init__(self, initiatives, mode="propose"):
        self.initiatives = initiatives
        self.mode = mode
        self.artifacts: dict[str, list[dict]] = {}
        self.recorded: list[dict] = []
        self.statuses: list[tuple[str, str]] = []
        self.spawned: list[tuple[str, str]] = []
        self.updates: list[tuple[str, dict]] = []
        self.pr_infos: dict[str, dict | None] = {}
        self.spawn_result = SimpleNamespace(id="sess-2")

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

    async def pr_lookup(self, art):
        return self.pr_infos.get(art["id"])

    async def update_artifact(self, artifact_id, **fields):
        self.updates.append((artifact_id, fields))

    async def read_config(self):
        return self.mode, {}


def _loop(rec):
    ctx = AutomationContext("sdlc-synthesizer")
    return SdlcSynthesizerLoop(
        ctx, list_initiatives=rec.list_initiatives, list_artifacts=rec.list_artifacts,
        record_artifact=rec.record_artifact, set_status=rec.set_status,
        spawn=rec.spawn, pr_lookup=rec.pr_lookup, update_artifact=rec.update_artifact,
        read_config=rec.read_config, clock=lambda: 0.0)


def _ini(stage, status="active"):
    return {"id": "i1", "slug": "dark-mode", "repo": "o/r", "title": "Dark mode",
            "stage": stage, "status": status, "origin": "human", "origin_ref": None}


@pytest.mark.asyncio
async def test_design_stage_proposes_spec():
    rec = Recorder([_ini("design")])
    await _loop(rec).tick()
    assert rec.spawned == [("dark-mode", "spec")]
    assert rec.recorded[0]["kind"] == "spec"
    assert rec.recorded[0]["repo_path"] == "docs/sdlc/dark-mode/spec.md"
    assert rec.statuses == [("i1", "awaiting_human")]


@pytest.mark.asyncio
async def test_plan_stage_proposes_plan():
    rec = Recorder([_ini("plan")])
    rec.artifacts["i1"] = [
        {"id": "a1", "kind": "intent", "pr_number": 1, "pr_url": "u",
         "commit_sha": "c1", "session_id": "s0"},
        {"id": "a2", "kind": "spec", "pr_number": 2, "pr_url": "u",
         "commit_sha": "c2", "session_id": "s1"},
    ]
    await _loop(rec).tick()
    assert rec.spawned == [("dark-mode", "plan")]


@pytest.mark.asyncio
async def test_intake_stage_is_not_the_synthesizers_job():
    rec = Recorder([_ini("intake")])
    await _loop(rec).tick()
    assert rec.spawned == []


@pytest.mark.asyncio
async def test_existing_stage_artifact_not_reproposed():
    rec = Recorder([_ini("design")])
    rec.artifacts["i1"] = [{"id": "a2", "kind": "spec", "pr_number": None,
                            "pr_url": None, "commit_sha": None, "session_id": "s1"}]
    await _loop(rec).tick()
    assert rec.spawned == []


@pytest.mark.asyncio
async def test_enrichment_fills_pr_then_sha_on_merge():
    art = {"id": "a2", "kind": "spec", "pr_number": None, "pr_url": None,
           "commit_sha": None, "session_id": "s1"}
    rec = Recorder([_ini("design")])
    rec.artifacts["i1"] = [art]
    rec.pr_infos["a2"] = {"number": 41, "url": "https://x/41", "state": "open",
                          "sha": None}
    await _loop(rec).tick()
    assert rec.updates == [("a2", {"pr_number": 41, "pr_url": "https://x/41"})]

    rec.updates.clear()
    art["pr_number"], art["pr_url"] = 41, "https://x/41"
    rec.pr_infos["a2"] = {"number": 41, "url": "https://x/41", "state": "merged",
                          "sha": "deadbeef"}
    await _loop(rec).tick()
    assert rec.updates == [("a2", {"commit_sha": "deadbeef"})]


@pytest.mark.asyncio
async def test_enriched_artifact_is_left_alone():
    rec = Recorder([_ini("design")])
    rec.artifacts["i1"] = [{"id": "a2", "kind": "spec", "pr_number": 41,
                            "pr_url": "u", "commit_sha": "deadbeef",
                            "session_id": "s1"}]
    await _loop(rec).tick()
    assert rec.updates == []


def test_automation_contract():
    a = SdlcSynthesizer()
    assert a.id == "sdlc-synthesizer"
    assert a.modes == ("off", "propose", "auto")
