"""SdlcSynthesizerLoop (#800): design→spec, plan→plan proposals + PR enrichment."""
from types import SimpleNamespace

import pytest

from server.automations.context import AutomationContext
from server.automations.sdlc_synthesizer import SdlcSynthesizer, SdlcSynthesizerLoop


class Ctx:
    """Records the activity events the loop emits (the feed is a seam too)."""

    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []

    async def emit(self, level, summary, **kw):
        self.events.append((level, summary, kw))


class Recorder:
    def __init__(self, initiatives, mode="propose"):
        self.initiatives = initiatives
        self.mode = mode
        self.artifacts: dict[str, list[dict]] = {}
        self.recorded: list[dict] = []
        self.statuses: list[tuple[str, str]] = []
        self.spawned: list[tuple[str, str]] = []
        self.materialized: list[str] = []
        self.updates: list[tuple[str, dict]] = []
        self.pr_infos: dict[str, dict | None] = {}
        self.spawn_result = SimpleNamespace(id="sess-2")
        self.materialize_result = SimpleNamespace(id="sess-9")
        self.pending_materialization: set[str] = set()
        self.attempts: dict[str, int] = {}

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

    async def spawn_materialize(self, ini):
        self.materialized.append(ini["slug"])
        return self.materialize_result

    async def has_pending_materialization(self, initiative_id):
        return initiative_id in self.pending_materialization

    async def materialization_attempts(self, initiative_id):
        return self.attempts.get(initiative_id, 0)

    async def pr_lookup(self, art):
        return self.pr_infos.get(art["id"])

    async def update_artifact(self, artifact_id, **fields):
        self.updates.append((artifact_id, fields))

    async def read_config(self):
        return self.mode, {}


def _loop(rec, ctx=None, *, materialize=True):
    return SdlcSynthesizerLoop(
        ctx or AutomationContext("sdlc-synthesizer"),
        list_initiatives=rec.list_initiatives, list_artifacts=rec.list_artifacts,
        record_artifact=rec.record_artifact, set_status=rec.set_status,
        spawn=rec.spawn,
        spawn_materialize=rec.spawn_materialize if materialize else None,
        has_pending_materialization=rec.has_pending_materialization,
        materialization_attempts=rec.materialization_attempts,
        pr_lookup=rec.pr_lookup, update_artifact=rec.update_artifact,
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


def _merged_plan(rec):
    """A plan-stage initiative whose plan artifact is merged and has no epic —
    the exact state the engine answers ``Materialize`` for (spec 003 §7b)."""
    rec.artifacts["i1"] = [
        {"id": "a1", "kind": "intent", "pr_number": 1, "pr_url": "u",
         "commit_sha": "c1", "session_id": "s0"},
        {"id": "a2", "kind": "spec", "pr_number": 2, "pr_url": "u",
         "commit_sha": "c2", "session_id": "s1"},
        {"id": "a3", "kind": "plan", "pr_number": 3, "pr_url": "u",
         "commit_sha": "c3", "session_id": "s2"},
    ]
    return rec


@pytest.mark.asyncio
async def test_merged_plan_spawns_a_decompose_session():
    rec = _merged_plan(Recorder([_ini("plan")]))
    ctx = Ctx()
    await _loop(rec, ctx).tick()
    assert rec.materialized == ["dark-mode"]
    assert rec.spawned == []  # the plan artifact exists — nothing to propose
    assert rec.statuses == [("i1", "awaiting_human")]
    kinds = [e[2].get("kind") for e in ctx.events]
    assert "materialization_proposed" in kinds
    ev = next(e for e in ctx.events if e[2].get("kind") == "materialization_proposed")
    assert "dark-mode" in ev[1] and ev[2].get("session_id") == "sess-9"


@pytest.mark.asyncio
async def test_a_parked_plan_still_materializes():
    """C4-R13a: the plan Propose already parked the initiative at
    ``awaiting_human`` and only the human Advance tap unparks it (skipping past
    ``plan``), so gating the decompose spawn on the STATUS would mean no plan is
    ever materialized. The park signal is the pending proposal, not the status."""
    rec = _merged_plan(Recorder([_ini("plan", status="awaiting_human")]))
    await _loop(rec).tick()
    assert rec.materialized == ["dark-mode"]


@pytest.mark.asyncio
async def test_pending_proposal_blocks_a_second_decompose():
    """A proposal awaiting the human gate IS the park: no second session, no
    second proposal to choose between."""
    rec = _merged_plan(Recorder([_ini("plan", status="awaiting_human")]))
    rec.pending_materialization.add("i1")
    ctx = Ctx()
    await _loop(rec, ctx).tick()
    assert rec.materialized == []
    assert rec.statuses == []
    assert [e for e in ctx.events if e[2].get("kind") == "materialization_proposed"] == []


@pytest.mark.asyncio
async def test_a_live_decompose_session_is_not_rechained():
    """While the decompose session is still running the seam answers None (it
    checks its own dedupe key first) — so the tick must not re-park, re-emit or
    schedule a second ingest chain."""
    rec = _merged_plan(Recorder([_ini("plan")]))
    ctx = Ctx()
    loop = _loop(rec, ctx)
    await loop.tick()
    rec.materialize_result = None  # the seam sees its own live session
    await loop.tick()
    assert rec.statuses == [("i1", "awaiting_human")]
    proposed = [e for e in ctx.events if e[2].get("kind") == "materialization_proposed"]
    assert len(proposed) == 1


@pytest.mark.asyncio
async def test_materialize_without_the_seam_warns_once():
    rec = _merged_plan(Recorder([_ini("plan")]))
    ctx = Ctx()
    loop = _loop(rec, ctx, materialize=False)
    await loop.tick()
    await loop.tick()
    assert rec.materialized == []
    assert rec.statuses == []
    warns = [e for e in ctx.events if e[0] == "warn"]
    assert len(warns) == 1


@pytest.mark.asyncio
async def test_refused_materialize_spawn_leaves_the_status_alone():
    """No session → no proposal is coming, so the initiative must stay pickable
    on the next tick rather than parking at ``awaiting_human`` forever."""
    rec = _merged_plan(Recorder([_ini("plan")]))
    rec.materialize_result = None
    await _loop(rec).tick()
    assert rec.materialized == ["dark-mode"]
    assert rec.statuses == []


@pytest.mark.asyncio
async def test_spent_decomposition_attempts_block_instead_of_respawning():
    """C4-R19c: a parse failure now leaves a REJECTED proposal rather than a
    blocked initiative, which is what lets the next tick try again — so the cap is
    what stops "again" from being every tick forever."""
    from server.automations.sdlc_synthesizer import MATERIALIZE_MAX_ATTEMPTS

    rec = _merged_plan(Recorder([_ini("plan", status="awaiting_human")]))
    rec.attempts["i1"] = MATERIALIZE_MAX_ATTEMPTS
    ctx = Ctx()
    loop = _loop(rec, ctx)
    await loop.tick()
    await loop.tick()
    assert rec.materialized == []
    assert rec.statuses == [("i1", "blocked"), ("i1", "blocked")]
    # Warned ONCE: the condition persists, so an unlatched emit would repeat on
    # every tick a human un-blocks into.
    warns = [e for e in ctx.events
             if e[2].get("kind") == "materialization_attempts_exhausted"]
    assert len(warns) == 1
    assert warns[0][0] == "warn"
    assert warns[0][2]["detail"] == {"attempts": MATERIALIZE_MAX_ATTEMPTS,
                                     "max": MATERIALIZE_MAX_ATTEMPTS}


@pytest.mark.asyncio
async def test_attempts_under_the_cap_still_decompose():
    from server.automations.sdlc_synthesizer import MATERIALIZE_MAX_ATTEMPTS

    rec = _merged_plan(Recorder([_ini("plan", status="awaiting_human")]))
    rec.attempts["i1"] = MATERIALIZE_MAX_ATTEMPTS - 1
    await _loop(rec).tick()
    assert rec.materialized == ["dark-mode"]


@pytest.mark.asyncio
async def test_a_pending_proposal_is_not_a_spent_attempt():
    """The count only decides anything once nothing is pending: a proposal waiting
    on the human gate is the outcome the loop wanted, not a failed attempt."""
    rec = _merged_plan(Recorder([_ini("plan", status="awaiting_human")]))
    rec.attempts["i1"] = 9
    rec.pending_materialization.add("i1")
    ctx = Ctx()
    await _loop(rec, ctx).tick()
    assert rec.materialized == []
    assert rec.statuses == []  # never blocked
    assert [e for e in ctx.events
            if e[2].get("kind") == "materialization_attempts_exhausted"] == []


def test_to_views_carries_the_epic_pointer():
    """The engine's Materialize decision hangs off ``InitiativeView.epic`` (#992),
    so the projection must fold epic_repo/epic_number into the ``owner/repo#N``
    ref — otherwise every initiative would look un-materialized forever."""
    from server.automations.sdlc_synthesizer import _to_views

    ini = dict(_ini("plan"), epic_repo="o/r", epic_number=42)
    view, _arts = _to_views(ini, [])
    assert view.epic == "o/r#42"


def test_to_views_epic_is_none_without_an_epic_number():
    from server.automations.sdlc_synthesizer import _to_views

    ini = dict(_ini("plan"), epic_repo=None, epic_number=None)
    view, _arts = _to_views(ini, [])
    assert view.epic is None


def test_automation_contract():
    a = SdlcSynthesizer()
    assert a.id == "sdlc-synthesizer"
    assert a.modes == ("off", "propose", "auto")

@pytest.mark.asyncio
async def test_observation_reports_gate_then_real_zero_and_failure():
    from server.automations.context import observed_status

    ctx = Ctx()
    rec = Recorder([_ini("design")])
    rec.artifacts["i1"] = [{"kind": "spec", "commit_sha": "merged"}]
    loop = _loop(rec, ctx)
    await loop.tick()
    status = observed_status(ctx, SdlcSynthesizer().status(ctx))
    assert status.waiting_reason == "human"
    assert status.level == "idle"
    assert status.counts["awaiting"] == 1
    assert status.observed_at
    rec.initiatives = []
    await loop.tick()
    status = SdlcSynthesizer().status(ctx)
    assert status.waiting_reason is None
    assert status.counts == {"initiatives": 0, "proposed": 0, "awaiting": 0}
    timestamp = ctx.observed_at

    async def failed():
        raise RuntimeError("database unavailable")

    loop._list_initiatives = failed
    await loop.tick()
    status = observed_status(ctx, SdlcSynthesizer().status(ctx))
    assert status.level == "unknown"
    assert status.observed_at == timestamp
    assert status.last_refresh_failed_at


@pytest.mark.asyncio
async def test_pending_materialization_is_human_wait_without_respawn():
    ctx = Ctx()
    rec = Recorder([_ini("plan")])
    rec.artifacts["i1"] = [{"kind": "plan", "commit_sha": "merged"}]
    rec.pending_materialization.add("i1")
    await _loop(rec, ctx).tick()
    assert SdlcSynthesizer().status(ctx).waiting_reason == "human"
    assert rec.materialized == []


@pytest.mark.asyncio
async def test_build_missing_issue_is_human_wait_but_later_stage_handoff_is_not():
    ctx = Ctx()
    rec = Recorder([_ini("build")])
    loop = _loop(rec, ctx)
    await loop.tick()
    status = SdlcSynthesizer().status(ctx)
    assert status.waiting_reason == "human"
    assert status.counts["awaiting"] == 1
    rec.initiatives = [_ini("deploy")]
    await loop.tick()
    assert SdlcSynthesizer().status(ctx).waiting_reason is None
