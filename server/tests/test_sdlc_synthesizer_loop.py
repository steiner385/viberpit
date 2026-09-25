"""SdlcSynthesizerLoop (#800): design→spec, plan→plan proposals + PR enrichment."""
from types import SimpleNamespace

import pytest

from server.automations.sdlc_synthesizer import (
    MaterializeAttempts, SdlcSynthesizer, SdlcSynthesizerLoop)


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
        self.policies: dict[str, dict] = {}
        #: session ids the daemon still holds live; an artifact row whose session
        #: is not in here holds no front-chain slot (spec 2026-09-14 §6, amended).
        self.live: set[str] = set()
        #: (initiative_id, from, to, record) per executed Advance; `advance_result`
        #: False simulates the compare-and-set losing to a human (spec §15).
        self.advanced: list[tuple[str, str, str, dict]] = []
        self.advance_result = True
        #: The kill switch (spec D9); tests that exercise a gate turn it on.
        self.autoresolve = False
        #: (artifact_id, repo, pr_number) per arm attempt; `arm_result` False
        #: simulates gh refusing (repo has auto-merge disabled).
        self.armed: list[tuple[str, str, int]] = []
        self.arm_result: dict = {"armed": True, "error": ""}
        #: initiative_id -> the last `auto_gate_record` persisted by a real
        #: arm attempt (mirrors `_sdlc_arm_auto_merge` flipping `outcome` to
        #: "refused" before persisting when gh declines — never on the unwired
        #: or already-armed paths, which never call this seam at all).
        self.last_auto_gate: dict[str, dict] = {}
        #: emitted activity events, so a test can assert `gate_refused`. Doubles
        #: as the default fake ctx (see `_loop`) so a test that omits `ctx` can
        #: still inspect what the loop emitted via `rec.events`.
        self.events: list[dict] = []

    async def advance_stage(self, iid, from_stage, to_stage, record):
        self.advanced.append((iid, from_stage, to_stage, record))
        return self.advance_result

    async def arm_auto_merge(self, artifact_id, repo, pr_number, initiative_id, gate):
        self.armed.append((artifact_id, repo, int(pr_number)))
        armed = bool(self.arm_result.get("armed"))
        self.last_auto_gate[initiative_id] = dict(gate) if armed else {**gate, "outcome": "refused"}
        return self.arm_result

    async def emit(self, level, summary, **kw):
        self.events.append({"level": level, "summary": summary, **kw})

    def autoresolve_enabled(self):
        return self.autoresolve

    async def read_policies(self):
        return self.policies

    async def live_sessions(self, ids):
        return self.live & set(ids)

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
        # `self.attempts` stays a plain int per initiative — the BUDGET, which is
        # what every case here is about. The real seam derives it from the total
        # net of the retry baseline (#1582); with no baseline the two are equal,
        # so folding both out of one number keeps these cases reading the same.
        return MaterializeAttempts.of(self.attempts.get(initiative_id, 0), None)

    async def pr_lookup(self, art):
        return self.pr_infos.get(art["id"])

    async def update_artifact(self, artifact_id, **fields):
        self.updates.append((artifact_id, fields))

    async def read_config(self):
        return self.mode, {}


def _loop(rec, ctx=None, *, materialize=True, advance=True, arm=True, clock=None):
    # `rec` doubles as the default fake ctx (it has `emit`): a test that omits
    # `ctx` can still inspect `rec.events`, matching every other Recorder seam.
    return SdlcSynthesizerLoop(
        ctx or rec,
        list_initiatives=rec.list_initiatives, list_artifacts=rec.list_artifacts,
        record_artifact=rec.record_artifact, set_status=rec.set_status,
        spawn=rec.spawn,
        spawn_materialize=rec.spawn_materialize if materialize else None,
        has_pending_materialization=rec.has_pending_materialization,
        materialization_attempts=rec.materialization_attempts,
        pr_lookup=rec.pr_lookup, update_artifact=rec.update_artifact,
        read_config=rec.read_config, clock=clock or (lambda: 0.0),
        read_policies=rec.read_policies,
        live_sessions=rec.live_sessions,
        advance_stage=rec.advance_stage if advance else None,
        autoresolve_enabled=rec.autoresolve_enabled,
        arm_auto_merge=rec.arm_auto_merge if arm else None)


def _ini(stage, status="active", **extra):
    return {"id": "i1", "slug": "dark-mode", "repo": "o/r", "title": "Dark mode",
            "stage": stage, "status": status, "origin": "human", "origin_ref": None,
            **extra}


@pytest.mark.asyncio
async def test_design_stage_proposes_spec():
    rec = Recorder([_ini("design")])
    await _loop(rec).tick()
    assert rec.spawned == [("dark-mode", "spec")]
    assert rec.recorded[0]["kind"] == "spec"
    assert rec.recorded[0]["repo_path"] == "docs/sdlc/dark-mode/spec.md"
    assert rec.statuses == [("i1", "awaiting_human")]


@pytest.mark.asyncio
async def test_design_stage_records_no_upstream_ref_by_default():
    """The common case (a real committed intent.md) — the spawn result carries
    no ``upstream_ref``, and the recorded artifact reflects that (#2159)."""
    rec = Recorder([_ini("design")])
    await _loop(rec).tick()
    assert rec.recorded[0]["upstream_ref"] is None


@pytest.mark.asyncio
async def test_design_stage_forwards_an_epic_adoption_upstream_ref():
    """When ``spawn`` seeded the prompt from an epic (no intent.md existed —
    #2159), it marks the returned session with ``upstream_ref``; the loop must
    carry that onto the recorded artifact row so the initiative page can show
    where the spec actually came from."""
    rec = Recorder([_ini("design", origin="epic-adoption")])
    rec.spawn_result = SimpleNamespace(id="sess-2", upstream_ref="o/r#42")
    await _loop(rec).tick()
    assert rec.recorded[0]["upstream_ref"] == "o/r#42"


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
    # `totalAttempts` rides beside the budget so the feed says which number the
    # cap was measured against (they differ after a retry — #1582).
    assert warns[0][2]["detail"] == {"attempts": MATERIALIZE_MAX_ATTEMPTS,
                                     "totalAttempts": MATERIALIZE_MAX_ATTEMPTS,
                                     "max": MATERIALIZE_MAX_ATTEMPTS}


@pytest.mark.asyncio
async def test_a_retry_re_arms_the_exhaustion_warning():
    """#1582: the warn latch must re-arm after a "Decompose again".

    It cannot be re-armed from outside — the API process that records the retry is
    not this one, and in remote mode not even this machine — so it is keyed on the
    TOTAL attempt count rather than the slug. Same initiative, same exhausted
    verdict, a higher total: the human hears about it a second time instead of the
    row going quietly back to `blocked`."""
    from server.automations.sdlc_synthesizer import MATERIALIZE_MAX_ATTEMPTS

    rec = _merged_plan(Recorder([_ini("plan", status="awaiting_human")]))
    ctx = Ctx()
    loop = _loop(rec, ctx)
    # First exhaustion: three attempts spent, none of them forgiven.
    rec.attempts["i1"] = MATERIALIZE_MAX_ATTEMPTS
    await loop.tick()
    await loop.tick()   # latched — still one warn

    def _warns():
        return [e for e in ctx.events
                if e[2].get("kind") == "materialization_attempts_exhausted"]
    assert len(_warns()) == 1

    # A retry, then three more failures: the baseline forgave the first three, so
    # the BUDGET is back at the cap while the TOTAL has doubled.
    async def _after_retry(_iid):
        return MaterializeAttempts.of(MATERIALIZE_MAX_ATTEMPTS * 2, MATERIALIZE_MAX_ATTEMPTS)
    loop._materialization_attempts = _after_retry
    await loop.tick()
    assert len(_warns()) == 2
    assert _warns()[1][2]["detail"] == {"attempts": MATERIALIZE_MAX_ATTEMPTS,
                                       "totalAttempts": MATERIALIZE_MAX_ATTEMPTS * 2,
                                       "max": MATERIALIZE_MAX_ATTEMPTS}
    await loop.tick()   # …and latched again at the new total
    assert len(_warns()) == 2


@pytest.mark.asyncio
async def test_a_retry_baseline_buys_attempts_back():
    """The budget, not the total, is what the cap is compared against: an
    initiative with the cap's worth of TOTAL proposals all forgiven by a retry
    baseline decomposes again rather than re-blocking (#1582)."""
    from server.automations.sdlc_synthesizer import MATERIALIZE_MAX_ATTEMPTS

    rec = _merged_plan(Recorder([_ini("plan", status="awaiting_human")]))

    async def _all_forgiven(_iid):
        return MaterializeAttempts.of(MATERIALIZE_MAX_ATTEMPTS, MATERIALIZE_MAX_ATTEMPTS)
    loop = _loop(rec)
    loop._materialization_attempts = _all_forgiven
    await loop.tick()
    assert rec.materialized == ["dark-mode"]
    assert ("i1", "blocked") not in rec.statuses


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
    assert a.modes == ("off", "on")


def test_fold_synth_mode_maps_legacy_modes_to_on():
    from server.automations.sdlc_synthesizer import LEGACY_SYNTH_MODES, fold_synth_mode
    assert LEGACY_SYNTH_MODES == ("propose", "auto")
    assert fold_synth_mode("propose") == "on"
    assert fold_synth_mode("auto") == "on"
    assert fold_synth_mode("on") == "on"
    assert fold_synth_mode("off") == "off"      # a human's explicit park survives

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
    assert status.counts == {"initiatives": 0, "proposed": 0, "awaiting": 0, "queued": 0,
                             "auto_resolved": 0}
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


def _prio(iid, slug, stage, priority, project="viberpit"):
    return {"id": iid, "slug": slug, "repo": "o/r", "title": slug, "stage": stage,
            "status": "active", "origin": "human", "origin_ref": None,
            "project_slug": project, "priority": priority, "epic_total": 0, "epic_done": 0}


@pytest.mark.asyncio
async def test_visits_in_the_order_the_loader_sorted_and_caps_new_spawns_per_product():
    """Two design-stage initiatives, cap 1: only the first (higher priority, first
    in the list) spawns; the second is counted as queued, not proposed."""
    rec = Recorder([_prio("i1", "hot", "design", 40.0), _prio("i2", "cold", "design", 2.0)])
    rec.policies = {"viberpit": {"max_initiatives_in_flight": 1}}
    ctx = Ctx()
    await _loop(rec, ctx).tick()
    assert rec.spawned == [("hot", "spec")]
    assert SdlcSynthesizer().status(ctx).counts["queued"] == 1
    assert SdlcSynthesizer().status(ctx).counts["proposed"] == 1


@pytest.mark.asyncio
async def test_an_initiative_already_producing_counts_against_the_cap():
    rec = Recorder([_prio("i1", "busy", "design", 40.0), _prio("i2", "next", "design", 30.0)])
    rec.artifacts["i1"] = [{"id": "a1", "kind": "spec", "commit_sha": None, "session_id": "s1"}]
    rec.live = {"s1"}
    rec.policies = {"viberpit": {"max_initiatives_in_flight": 1}}
    await _loop(rec).tick()
    assert rec.spawned == []                       # i1 is producing; i2 queued behind it


@pytest.mark.asyncio
async def test_an_intake_stage_initiative_never_holds_a_front_chain_slot():
    """Intake is governed by sdlc-intake's own daily budget, not by this cap, so
    an open intent PR with a live session must not starve a design-stage spec."""
    rec = Recorder([_prio("i1", "intaking", "intake", 40.0), _prio("i2", "next", "design", 30.0)])
    rec.artifacts["i1"] = [{"id": "a1", "kind": "intent", "commit_sha": None, "session_id": "s1"}]
    rec.live = {"s1"}
    rec.policies = {"viberpit": {"max_initiatives_in_flight": 1}}
    await _loop(rec).tick()
    assert rec.spawned == [("next", "spec")]


@pytest.mark.asyncio
async def test_a_blocked_initiative_never_holds_a_front_chain_slot():
    """The loop refuses to act on a blocked initiative (NeedsHuman), so it is not
    *actively* producing and must not occupy capacity."""
    rec = Recorder([_prio("i1", "stuck", "design", 40.0) | {"status": "blocked"},
                    _prio("i2", "next", "design", 30.0)])
    rec.artifacts["i1"] = [{"id": "a1", "kind": "spec", "commit_sha": None, "session_id": "s1"}]
    rec.live = {"s1"}
    rec.policies = {"viberpit": {"max_initiatives_in_flight": 1}}
    await _loop(rec).tick()
    assert rec.spawned == [("next", "spec")]


@pytest.mark.asyncio
async def test_a_dead_artifact_session_does_not_leak_a_slot():
    """An unmerged spec row whose session died without opening a PR would otherwise
    hold the slot forever — there is no reaper for it."""
    rec = Recorder([_prio("i1", "dead", "design", 40.0), _prio("i2", "next", "design", 30.0)])
    rec.artifacts["i1"] = [{"id": "a1", "kind": "spec", "commit_sha": None, "session_id": "s1"}]
    rec.live = set()                               # s1 is gone
    rec.policies = {"viberpit": {"max_initiatives_in_flight": 1}}
    await _loop(rec).tick()
    assert rec.spawned == [("next", "spec")]


@pytest.mark.asyncio
async def test_a_capped_product_emits_exactly_one_ceiling_event_per_tick():
    """Two rows queued behind one cap: the operator gets one `ceiling` event for
    the product, not one per queued initiative."""
    rec = Recorder([_prio("i1", "a", "design", 9.0), _prio("i2", "b", "design", 8.0),
                    _prio("i3", "c", "design", 7.0)])
    rec.policies = {"viberpit": {"max_initiatives_in_flight": 1}}
    ctx = Ctx()
    await _loop(rec, ctx).tick()
    ceilings = [e for e in ctx.events if e[2].get("kind") == "ceiling"]
    assert len(ceilings) == 1
    assert ceilings[0][0] == "info"
    assert "viberpit" in ceilings[0][1] and "cap 1" in ceilings[0][1]
    assert ceilings[0][2]["target"] == "b"         # the first row that had to queue


@pytest.mark.asyncio
async def test_a_merged_and_parked_initiative_does_not_hold_a_slot():
    rec = Recorder([_prio("i1", "parked", "design", 40.0), _prio("i2", "next", "design", 30.0)])
    rec.artifacts["i1"] = [{"id": "a1", "kind": "spec", "commit_sha": "cafe", "session_id": "s1"}]
    rec.policies = {"viberpit": {"max_initiatives_in_flight": 1}}
    await _loop(rec).tick()
    assert rec.spawned == [("next", "spec")]


@pytest.mark.asyncio
async def test_cap_zero_pauses_new_artifacts_but_never_materialization():
    rec = Recorder([_prio("i1", "plan-done", "plan", 5.0), _prio("i2", "fresh", "design", 50.0)])
    rec.artifacts["i1"] = [{"id": "a1", "kind": "plan", "commit_sha": "cafe", "session_id": "s1"}]
    rec.policies = {"viberpit": {"max_initiatives_in_flight": 0}}
    await _loop(rec).tick()
    assert rec.spawned == []
    assert rec.materialized == ["plan-done"]


@pytest.mark.asyncio
async def test_products_are_capped_independently_and_a_homeless_initiative_uses_defaults():
    rec = Recorder([_prio("i1", "a", "design", 9.0, project="viberpit"),
                    _prio("i2", "b", "design", 8.0, project="kindash"),
                    _prio("i3", "c", "design", 7.0, project=None)])
    rec.policies = {"viberpit": {"max_initiatives_in_flight": 1}, "kindash": {"max_initiatives_in_flight": 1}}
    await _loop(rec).tick()
    assert rec.spawned == [("a", "spec"), ("b", "spec"), ("c", "spec")]


def test_is_producing_rule():
    from server.automations.sdlc_synthesizer import is_producing
    spec = {"kind": "spec", "commit_sha": None, "session_id": "s1"}
    assert is_producing("design", [spec], {"s1"}) is True
    assert is_producing("design", [spec], set()) is False        # session died — no slot leak
    assert is_producing("design", [{"kind": "spec", "commit_sha": "cafe",
                                    "session_id": "s1"}], {"s1"}) is False   # merged
    assert is_producing("design", [{"kind": "intent", "commit_sha": None,
                                    "session_id": "s1"}], {"s1"}) is False   # other stage's kind
    assert is_producing("intake", [{"kind": "intent", "commit_sha": None,
                                    "session_id": "s1"}], {"s1"}) is False   # intake has its own budget
    assert is_producing("build", [{"kind": "plan", "commit_sha": None,
                                   "session_id": "s1"}], {"s1"}) is False    # not a front-chain stage
    assert is_producing("design", [], {"s1"}) is False


def test_to_views_carries_epic_progress():
    from server.automations.sdlc_synthesizer import _to_views
    view, _ = _to_views(_prio("i1", "x", "build", None) | {"epic_total": 4, "epic_done": 3}, [])
    assert (view.epic_total, view.epic_done) == (4, 3)
    legacy, _ = _to_views(_ini("build"), [])
    assert (legacy.epic_total, legacy.epic_done) == (0, 0)


# --------------------------------------------------------------------------- #
# Phase 2: executing the `stage.advance` gates under the product's tolerance    #
# --------------------------------------------------------------------------- #

def _merged_spec_at_design(project="viberpit"):
    rec = Recorder([_ini("design", "awaiting_human", project_slug=project)])
    rec.artifacts = {"i1": [{"id": "a1", "kind": "spec", "commit_sha": "abc", "pr_number": 7,
                             "session_id": "sess-1"}]}
    return rec


@pytest.mark.asyncio
async def test_a_merged_spec_advances_to_plan_under_balanced_and_is_recorded():
    """Under `balanced` the `stage.advance` gate resolves itself: ONE compare-and-set
    write moves design → plan, the record names the gate, and the tick counts it."""
    rec = _merged_spec_at_design()
    rec.policies = {"viberpit": {"risk_tolerance": "balanced"}}
    rec.autoresolve = True
    ctx = Ctx()
    await _loop(rec, ctx).tick()
    assert [(a[0], a[1], a[2]) for a in rec.advanced] == [("i1", "design", "plan")]
    record = rec.advanced[0][3]
    assert record["gate"] == "stage.advance" and record["tolerance"] == "balanced"
    assert record["detail"] == {"from": "design", "to": "plan"}
    assert rec.spawned == [] and rec.statuses == []      # the NEXT tick proposes the plan
    resolved = [e for e in ctx.events if e[2].get("kind") == "auto_resolved"]
    assert len(resolved) == 1 and resolved[0][2]["target"] == "dark-mode"
    assert SdlcSynthesizer().status(ctx).counts["auto_resolved"] == 1


@pytest.mark.asyncio
async def test_cautious_and_the_kill_switch_both_keep_the_human_gate():
    """The default policy parks exactly as phase 1 did; with the process-wide
    switch off a `bold` product reads as `cautious` (spec D9)."""
    for policies, autoresolve in (({}, True),
                                  ({"viberpit": {"risk_tolerance": "bold"}}, False)):
        rec = _merged_spec_at_design()
        rec.policies, rec.autoresolve = policies, autoresolve
        ctx = Ctx()
        await _loop(rec, ctx).tick()
        assert rec.advanced == []
        counts = SdlcSynthesizer().status(ctx).counts
        assert (counts["awaiting"], counts["auto_resolved"]) == (1, 0)


@pytest.mark.asyncio
async def test_a_stale_advance_is_not_counted_as_resolved():
    """The compare-and-set lost to a human's newer decision (spec §15): nothing
    is recorded as done, no event, and the row is simply awaiting again."""
    rec = _merged_spec_at_design()
    rec.policies = {"viberpit": {"risk_tolerance": "balanced"}}
    rec.autoresolve, rec.advance_result = True, False
    ctx = Ctx()
    await _loop(rec, ctx).tick()
    assert len(rec.advanced) == 1
    assert [e for e in ctx.events if e[2].get("kind") == "auto_resolved"] == []
    counts = SdlcSynthesizer().status(ctx).counts
    assert (counts["awaiting"], counts["auto_resolved"]) == (1, 0)


@pytest.mark.asyncio
async def test_a_fully_built_epic_advances_to_deploy_under_the_built_gate():
    rec = Recorder([_ini("build", "active", project_slug="viberpit", origin_ref="o/r#1",
                         epic_repo="o/r", epic_number=5, epic_total=3, epic_done=3)])
    rec.policies = {"viberpit": {"risk_tolerance": "balanced"}}
    rec.autoresolve = True
    await _loop(rec).tick()
    assert [(a[1], a[2], a[3]["gate"]) for a in rec.advanced] == [
        ("build", "deploy", "stage.advance_built")]


@pytest.mark.asyncio
async def test_an_unwired_advance_seam_warns_once_and_parks():
    """A deployment without the write seam must never resolve a gate by accident:
    the row stays awaiting and the operator hears about it once per initiative."""
    rec = _merged_spec_at_design()
    rec.policies = {"viberpit": {"risk_tolerance": "balanced"}}
    rec.autoresolve = True
    ctx = Ctx()
    loop = _loop(rec, ctx, advance=False)
    await loop.tick()
    await loop.tick()
    warns = [e for e in ctx.events if e[2].get("kind") == "advance_unwired"]
    assert len(warns) == 1 and warns[0][0] == "warn"
    assert rec.advanced == []


@pytest.mark.asyncio
async def test_an_open_artifact_pr_auto_merges_under_balanced():
    """`MergeArtifact` is decided under `balanced` and now executed (phase 2b):
    the arm-auto-merge seam arms the PR instead of parking it as awaiting."""
    rec = Recorder([_ini("design", "awaiting_human", project_slug="viberpit")])
    rec.artifacts = {"i1": [{"id": "a1", "kind": "spec", "commit_sha": None, "pr_number": 7,
                             "session_id": "sess-1", "auto_merge_armed_at": None}]}
    rec.policies = {"viberpit": {"risk_tolerance": "balanced"}}
    rec.autoresolve = True
    ctx = Ctx()
    await _loop(rec, ctx).tick()
    assert rec.advanced == [] and rec.spawned == []
    assert rec.armed == [("a1", "o/r", 7)]
    assert SdlcSynthesizer().status(ctx).counts["auto_resolved"] == 1
    assert SdlcSynthesizer().status(ctx).counts["awaiting"] == 0
    # spec §8: the gate is recorded, using the same helper `_advance` uses.
    record = rec.last_auto_gate["i1"]
    assert (record["gate"], record["outcome"], record["tolerance"]) == (
        "artifact.merge", "resolved", "balanced")
    assert record["detail"] == {"kind": "spec", "pr": 7}


# --------------------------------------------------------------------------- #
# MergeArtifact (spec 2026-09-14 §6) — phase 2b                                #
# --------------------------------------------------------------------------- #

def _merge_ready(rec, slug="alpha", iid="i-1"):
    """An initiative whose spec artifact has an OPEN PR: the engine returns
    MergeArtifact once the product's tolerance covers `artifact.merge`."""
    rec.policies["prod"] = {"risk_tolerance": "balanced", "dispatch": "auto"}
    rec.autoresolve = True
    rec.artifacts[iid] = [{"id": "a-1", "kind": "spec", "pr_number": 42,
                           "pr_url": "https://github.com/o/r/pull/42",
                           "commit_sha": None, "session_id": None,
                           "auto_merge_armed_at": None}]
    return rec


@pytest.mark.asyncio
async def test_merge_artifact_arms_auto_merge_once():
    rec = Recorder([{"id": "i-1", "slug": "alpha", "stage": "design",
                     "status": "active", "repo": "o/r", "project_slug": "prod"}],
                   mode="on")
    _merge_ready(rec)
    loop = _loop(rec)
    await loop.tick()

    assert rec.armed == [("a-1", "o/r", 42)]
    assert loop._ctx.sdlc_synthesizer_counts["auto_resolved"] == 1
    # spec §8: recorded with the same helper `_advance` uses, outcome "resolved".
    assert rec.last_auto_gate["i-1"]["gate"] == "artifact.merge"
    assert rec.last_auto_gate["i-1"]["outcome"] == "resolved"


@pytest.mark.asyncio
async def test_merge_artifact_refusal_records_gate_refused_and_does_not_stamp():
    rec = Recorder([{"id": "i-1", "slug": "alpha", "stage": "design",
                     "status": "active", "repo": "o/r", "project_slug": "prod"}],
                   mode="on")
    _merge_ready(rec)
    rec.arm_result = {"armed": False, "error": "Auto-merge is not allowed"}
    loop = _loop(rec)
    await loop.tick()

    assert rec.armed == [("a-1", "o/r", 42)]
    assert loop._ctx.sdlc_synthesizer_counts["auto_resolved"] == 0
    assert loop._ctx.sdlc_synthesizer_counts["awaiting"] == 1
    kinds = [e.get("kind") for e in rec.events]
    assert "gate_refused" in kinds
    # spec §8: a refusal STILL records the gate (outcome "refused") — the
    # `gates.py:_summary` "auto-merge unavailable" branch exists for this —
    # but never stamps `auto_merge_armed_at`, so a retry next tick is correct.
    assert rec.last_auto_gate["i-1"]["gate"] == "artifact.merge"
    assert rec.last_auto_gate["i-1"]["outcome"] == "refused"
    assert rec.artifacts["i-1"][0]["auto_merge_armed_at"] is None


@pytest.mark.asyncio
async def test_merge_artifact_skips_an_already_armed_pr():
    rec = Recorder([{"id": "i-1", "slug": "alpha", "stage": "design",
                     "status": "active", "repo": "o/r", "project_slug": "prod"}],
                   mode="on")
    _merge_ready(rec)
    rec.artifacts["i-1"][0]["auto_merge_armed_at"] = "2026-09-16T00:00:00+00:00"
    loop = _loop(rec)
    await loop.tick()

    assert rec.armed == []          # the one gh call is never spent twice
    assert loop._ctx.sdlc_synthesizer_counts["awaiting"] == 1


@pytest.mark.asyncio
async def test_an_unwired_arm_seam_warns_once_and_parks():
    """A deployment without the arm-auto-merge write seam must never resolve
    the gate by accident: the artifact stays awaiting and the operator hears
    about it once per initiative, mirroring the advance/materialize seams."""
    rec = Recorder([{"id": "i-1", "slug": "alpha", "stage": "design",
                     "status": "active", "repo": "o/r", "project_slug": "prod"}],
                   mode="on")
    _merge_ready(rec)
    ctx = Ctx()
    loop = _loop(rec, ctx, arm=False)
    await loop.tick()
    await loop.tick()
    warns = [e for e in ctx.events if e[2].get("kind") == "arm_unwired"]
    assert len(warns) == 1 and warns[0][0] == "warn"
    assert rec.armed == []
    assert SdlcSynthesizer().status(ctx).counts["awaiting"] == 1


@pytest.mark.asyncio
async def test_cautious_never_arms():
    rec = Recorder([{"id": "i-1", "slug": "alpha", "stage": "design",
                     "status": "active", "repo": "o/r", "project_slug": "prod"}],
                   mode="on")
    _merge_ready(rec)
    rec.policies["prod"]["risk_tolerance"] = "cautious"
    loop = _loop(rec)
    await loop.tick()

    assert rec.armed == []
    assert loop._ctx.sdlc_synthesizer_counts["awaiting"] == 1


@pytest.mark.asyncio
async def test_bold_with_the_kill_switch_off_never_arms():
    """The other half of `test_cautious_never_arms`: a permissive tolerance
    does not bypass the kill switch (spec D9) — with autoresolve off, `bold`
    still never arms, same as `cautious`."""
    rec = Recorder([{"id": "i-1", "slug": "alpha", "stage": "design",
                     "status": "active", "repo": "o/r", "project_slug": "prod"}],
                   mode="on")
    _merge_ready(rec)
    rec.policies["prod"]["risk_tolerance"] = "bold"
    rec.autoresolve = False
    loop = _loop(rec)
    await loop.tick()

    assert rec.armed == []
    assert loop._ctx.sdlc_synthesizer_counts["awaiting"] == 1


@pytest.mark.asyncio
async def test_a_refused_arm_is_not_retried_or_re_reported():
    """Whole-branch review finding 2: against a repo with auto-merge
    disabled, every tick (60s interval) used to cost a fresh `gh` call plus a
    `gate_refused` feed event, forever. Once refused, the slug is remembered
    for the rest of this process — same idiom as `_arm_unwired` — so a second
    tick spends neither."""
    rec = Recorder([{"id": "i-1", "slug": "alpha", "stage": "design",
                     "status": "active", "repo": "o/r", "project_slug": "prod"}],
                   mode="on")
    _merge_ready(rec)
    rec.arm_result = {"armed": False, "error": "Auto-merge is not allowed"}
    loop = _loop(rec)
    await loop.tick()
    await loop.tick()

    assert rec.armed == [("a-1", "o/r", 42)]           # gh spent exactly once
    kinds = [e.get("kind") for e in rec.events]
    assert kinds.count("gate_refused") == 1


@pytest.mark.asyncio
async def test_a_refused_arm_retries_after_the_ttl():
    """#2037: unlike `_arm_unwired`, `_arm_refused` latches a GitHub-side fact
    (the repo's auto-merge setting) an owner can flip at any moment with
    nothing in this process observing it. A tick within `ARM_REFUSED_RETRY_S`
    still suppresses the retry, but one past it spends a fresh `gh` call —
    letting a settings fix take effect without a daemon restart."""
    rec = Recorder([{"id": "i-1", "slug": "alpha", "stage": "design",
                     "status": "active", "repo": "o/r", "project_slug": "prod"}],
                   mode="on")
    _merge_ready(rec)
    rec.arm_result = {"armed": False, "error": "Auto-merge is not allowed"}
    now = [0.0]
    loop = _loop(rec, clock=lambda: now[0])
    await loop.tick()

    now[0] += 3600.0 - 1.0        # still within the TTL
    await loop.tick()
    assert rec.armed == [("a-1", "o/r", 42)]            # second tick suppressed

    now[0] += 1.0                 # TTL has now elapsed
    rec.arm_result = {"armed": True, "error": ""}
    await loop.tick()

    assert rec.armed == [("a-1", "o/r", 42), ("a-1", "o/r", 42)]
    kinds = [e.get("kind") for e in rec.events]
    assert kinds.count("gate_refused") == 1
    assert kinds.count("auto_resolved") == 1
