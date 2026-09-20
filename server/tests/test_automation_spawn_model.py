"""Spawn-site model wiring: backlog-runner and pr-doctor resolve default_model
config into the ``model=`` create kwarg (and stamp model/model_source into the
spawned-event detail). The assessor itself is covered in test_model_pick.py."""
from __future__ import annotations

import os
import types
from dataclasses import replace
from unittest.mock import patch

import pytest

from server.automations.backlog_selection import Batch
from server.github.pr_status import PullRequestInfo


def _capture_spawn(captured):
    async def fake_spawn(**kw):
        captured.update(kw)
        return types.SimpleNamespace(id="new-sess")
    return fake_spawn


# --------------------------------------------------------------------------- #
# backlog-runner (_backlog_spawn)                                              #
# --------------------------------------------------------------------------- #

def _build_backlog_orch(config, *, policies=None, tier_floor=None, ladder_rung=None,
                        accepted_decisions=None):
    """A bare orchestrator whose runner config is *config*.

    The session model is NOT read here since the fix wave: ``_backlog_spawn``
    takes it as a required ``default_model`` keyword — the loop passes the value
    from the tick's policy snapshot, the tap paths read it through
    ``_backlog_policy_model``. *policies* stubs that read for the few tests that
    exercise it.

    *tier_floor* stubs the ``_backlog_tier_floor`` seam (phase 4): the spawn asks
    it for the primary issue's capability floor, and every test here would
    otherwise reach for a DB. *ladder_rung* stubs ``_backlog_ladder_rung`` the
    same way (spec 2026-09-18 §4): the rung this dispatch is pinned to because
    the primary issue is parked ``stronger_model``, or ``None`` when it is not.
    *accepted_decisions* stubs ``_backlog_accepted_decisions`` (spec 2026-09-18
    §6): ref → the accepted decision text the implement prompt quotes, defaulting
    to none so no test here needs a seeded proposal.

    The dispatch KIND has no seam: it is read off ``Batch.primary_hold_kind``
    (spec 2026-09-18 §5), which the candidate pool put there and the loop stamped
    onto the claim intent, so a test picks it by building the batch it wants."""
    import server.sessions.orchestrator as orch_mod
    from server.automations.context import AutomationContext
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._backlog_ctx = AutomationContext("backlog-runner")

    async def read_config():
        return "on", config
    orch._backlog_read_config = read_config  # type: ignore[method-assign]

    async def load_policies():
        return dict(policies or {})
    orch._backlog_load_policies = load_policies  # type: ignore[method-assign]

    async def floor(product_slug, primary_ref):
        return tier_floor
    orch._backlog_tier_floor = floor  # type: ignore[method-assign]

    async def rung(product_slug, primary_ref, *, default_model, floor):
        return ladder_rung
    orch._backlog_ladder_rung = rung  # type: ignore[method-assign]

    async def decisions(refs):
        return dict(accepted_decisions or {})
    orch._backlog_accepted_decisions = decisions  # type: ignore[method-assign]

    return orch


def _assess_returning(tier):
    async def assess(task_summary, **kw):
        return tier
    return assess


def _patch_git(monkeypatch, tmp_path):
    import server.sessions.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod.gitrepos, "ensure_clone",
                        lambda root, repo, url: str(tmp_path))
    monkeypatch.setattr(orch_mod.gitrepos, "list_remote_branches",
                        lambda repo: {"default": "master"})


_BATCH = Batch(primary="o/r#7", issues=["o/r#7"], repo="o/r",
               titles={"o/r#7": "fix the frobnicator"}, product="widget")
# The same batch as it arrives when the primary is parked pending evidence: the
# candidate pool carries ``hold_kind`` onto the batch (spec 2026-09-18 §5).
_EVIDENCE_BATCH = replace(_BATCH, primary_hold_kind="evidence")


@pytest.mark.asyncio
async def test_backlog_spawn_passes_pinned_model(tmp_path, monkeypatch):
    orch = _build_backlog_orch({})
    _patch_git(monkeypatch, tmp_path)
    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    await orch._backlog_spawn(_BATCH, "acct", default_model="sonnet", ownership_token="test-token")
    assert captured["model"] == "sonnet"
    # `engine` joins the detail in Phase 7: a spawn record has to say which agent
    # ran it, or a mixed-engine backlog is unreadable after the fact.
    assert captured["detail"] == {"issues": ["o/r#7"], "model": "sonnet",
                                  "model_source": "pinned", "engine": "claude_code",
                                  "dispatch_kind": "implement"}


@pytest.mark.asyncio
async def test_backlog_spawn_resolves_the_default_branch_through_the_one_resolver(
        tmp_path, monkeypatch):
    """``_sdlc_default_branch`` is THE default-branch resolver for every spawn
    site in the orchestrator; a site with its own inline
    ``list_remote_branches(...).get("default") or "master"`` drifts from it
    silently (the prompts tell the agent never to assume ``master``)."""
    orch = _build_backlog_orch({})
    _patch_git(monkeypatch, tmp_path)
    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    seen: dict = {}

    async def fake_default_branch(repo):
        seen["repo"] = repo
        return "trunk"

    orch._sdlc_default_branch = fake_default_branch  # type: ignore[method-assign]
    await orch._backlog_spawn(_BATCH, "acct", default_model="auto", ownership_token="test-token")
    assert seen == {"repo": "o/r"}
    assert "trunk" in captured["initial_prompt"]


def test_no_spawn_site_resolves_the_default_branch_inline():
    """The sibling sweep for the test above: nothing in the orchestrator may
    re-derive the default branch from ``list_remote_branches`` itself."""
    import inspect

    import server.sessions.orchestrator as orch_mod

    src = inspect.getsource(orch_mod)
    assert 'list_remote_branches' not in src, (
        "resolve the default branch through self._sdlc_default_branch() "
        "(server/pm/map_changes.py:resolve_default_branch), not inline")


@pytest.mark.asyncio
async def test_backlog_spawn_auto_assesses_issue_titles(tmp_path, monkeypatch):
    import server.automations.model_pick as model_pick
    seen: dict = {}

    async def pick_haiku(task_summary, **kw):
        seen["summary"] = task_summary
        return "haiku"
    monkeypatch.setattr(model_pick, "assess_model", pick_haiku)

    orch = _build_backlog_orch({})
    _patch_git(monkeypatch, tmp_path)
    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    await orch._backlog_spawn(_BATCH, "acct", default_model="auto", ownership_token="test-token")
    assert "fix the frobnicator" in seen["summary"]  # titles reach the assessor
    assert captured["model"] == "haiku"
    assert captured["detail"]["model_source"] == "auto"


@pytest.mark.asyncio
async def test_backlog_spawn_default_keeps_account_default(tmp_path, monkeypatch):
    orch = _build_backlog_orch({})
    _patch_git(monkeypatch, tmp_path)
    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    await orch._backlog_spawn(_BATCH, "acct", default_model="default", ownership_token="test-token")
    assert captured["model"] is None
    # No model keys — but the engine is always recorded (Phase 7).
    assert captured["detail"] == {"issues": ["o/r#7"], "engine": "claude_code",
                                  "dispatch_kind": "implement"}


@pytest.mark.asyncio
async def test_tap_paths_read_the_products_model_and_survive_a_failing_read():
    """The loop hands ``default_model`` down from its tick snapshot; the propose
    tap and manual dispatch have no snapshot, so they resolve it through
    ``_backlog_policy_model`` — the ONE read left on a spawn path.

    A read that RAISES costs the model PREFERENCE, never the spawn: by the time a
    tap runs, the decision that this batch may run has already been taken, and
    ``auto`` (the schema default) is exactly what a pre-flip claim ran under."""
    from server.automations.product_policy import effective_policy
    orch = _build_backlog_orch({}, policies={
        "widget": effective_policy({"dispatch": "auto", "default_model": "opus"})})
    assert await orch._backlog_policy_model("widget") == "opus"
    assert await orch._backlog_policy_model("unknown-product") == "auto"
    assert await orch._backlog_policy_model(None) == "auto"

    async def boom():
        raise RuntimeError("policy read down (injected)")
    orch._backlog_load_policies = boom  # type: ignore[method-assign]
    assert await orch._backlog_policy_model("widget") == "auto"


def test_backlog_spawn_never_reads_policies_itself():
    """Sibling guard for the test above: a second read at the spawn site is a
    SECOND answer — policies can change between the dispatch decision and the
    spawn, and the re-read's own failure path silently downgraded the product's
    pinned model to ``auto``. The value travels with the claim instead."""
    import inspect

    import server.sessions.orchestrator as orch_mod

    src = inspect.getsource(orch_mod.SessionOrchestrator._backlog_spawn)
    assert "_backlog_load_policies" not in src and "effective_policy" not in src


# --------------------------------------------------------------------------- #
# backlog-runner tier floor at the spawn (policy phase 4, D9)                  #
# --------------------------------------------------------------------------- #

async def _audit_model_picks():
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import AuditEvent
    async with db_session() as db:
        return list((await db.execute(select(AuditEvent).where(
            AuditEvent.type == "backlog_runner.model_pick"))).scalars().all())


@pytest.mark.asyncio
async def test_backlog_spawn_raises_an_auto_pick_to_the_capabilitys_floor(
        tmp_path, monkeypatch):
    """D9: this capability's issues merged first-pass only at a higher tier, so
    the ``auto`` assessment is raised to that floor — evidence over a fresh
    guess. The spawn record says both what was assessed and what floored it."""
    import server.automations.model_pick as model_pick
    monkeypatch.setattr(model_pick, "assess_model", _assess_returning("sonnet"))

    orch = _build_backlog_orch({})
    seen: dict = {}

    async def floor(product_slug, primary_ref):
        seen.update(product=product_slug, ref=primary_ref)
        return "opus"
    orch._backlog_tier_floor = floor  # type: ignore[method-assign]
    _patch_git(monkeypatch, tmp_path)
    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    await orch._backlog_spawn(_BATCH, "acct", default_model="auto", ownership_token="test-token")

    # The floor is looked up for the PRIMARY issue's capability, in the batch's product.
    assert seen == {"product": "widget", "ref": "o/r#7"}
    assert captured["model"] == "opus"
    assert captured["detail"] == {
        "issues": ["o/r#7"], "engine": "claude_code", "model": "opus",
        "model_source": "auto+floor", "assessed": "sonnet", "floor": "opus",
        "dispatch_kind": "implement"}


@pytest.mark.asyncio
async def test_backlog_spawn_audits_an_auto_model_pick(isolated_db, tmp_path,
                                                       monkeypatch):
    """One durable row per auto pick: the metric for whether the assessor and
    the capability's own history agree."""
    import server.automations.model_pick as model_pick
    monkeypatch.setattr(model_pick, "assess_model", _assess_returning("sonnet"))

    orch = _build_backlog_orch({}, tier_floor="opus")
    _patch_git(monkeypatch, tmp_path)
    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    await orch._backlog_spawn(_BATCH, "acct", default_model="auto", ownership_token="test-token")

    rows = await _audit_model_picks()
    assert len(rows) == 1
    assert rows[0].identity == "backlog-runner"
    assert rows[0].detail == {
        "target": "o/r#7", "product": "widget", "assessed": "sonnet",
        "floor": "opus", "applied": "opus", "source": "auto+floor"}


@pytest.mark.asyncio
async def test_backlog_spawn_that_produces_no_session_is_unaudited(
        isolated_db, tmp_path, monkeypatch):
    """The audit is written AFTER the spawn, guarded on a real record: a refused
    or de-duplicated spawn (``spawn_session`` → ``None``) made no pick that was
    ever applied, and an orphan row would bias the ``assessed`` vs ``applied``
    metric the audit exists to carry."""
    import server.automations.model_pick as model_pick
    monkeypatch.setattr(model_pick, "assess_model", _assess_returning("sonnet"))

    orch = _build_backlog_orch({}, tier_floor="opus")
    _patch_git(monkeypatch, tmp_path)

    async def refuse(**kw):
        return None
    orch._backlog_ctx.spawn_session = refuse  # type: ignore[method-assign]
    assert await orch._backlog_spawn(_BATCH, "acct", default_model="auto", ownership_token="test-token") is None
    assert await _audit_model_picks() == []


@pytest.mark.asyncio
async def test_backlog_spawn_pinned_model_is_untouched_by_a_floor_and_unaudited(
        isolated_db, tmp_path, monkeypatch):
    """A pinned model is an operator's decision: no floor raises it, its spawn
    detail is exactly what it was, and there is no pick to audit."""
    orch = _build_backlog_orch({}, tier_floor="fable")
    _patch_git(monkeypatch, tmp_path)
    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    await orch._backlog_spawn(_BATCH, "acct", default_model="sonnet", ownership_token="test-token")

    assert captured["model"] == "sonnet"
    assert captured["detail"] == {"issues": ["o/r#7"], "model": "sonnet",
                                  "model_source": "pinned", "engine": "claude_code",
                                  "dispatch_kind": "implement"}
    assert await _audit_model_picks() == []


@pytest.mark.asyncio
async def test_backlog_spawn_survives_a_failing_floor_lookup(tmp_path, monkeypatch):
    """§13: the history read is best-effort. A seam that RAISES costs the floor,
    never the spawn — the assessed model is still applied."""
    import server.automations.model_pick as model_pick
    monkeypatch.setattr(model_pick, "assess_model", _assess_returning("sonnet"))

    orch = _build_backlog_orch({})

    async def boom(product_slug, primary_ref):
        raise RuntimeError("outcome history read down (injected)")
    orch._backlog_tier_floor = boom  # type: ignore[method-assign]
    _patch_git(monkeypatch, tmp_path)
    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    await orch._backlog_spawn(_BATCH, "acct", default_model="auto", ownership_token="test-token")

    assert captured["model"] == "sonnet"
    assert captured["detail"]["model_source"] == "auto"
    assert "floor" not in captured["detail"]


@pytest.mark.asyncio
async def test_tap_dispatch_applies_the_floor_through_the_one_spawn_path(
        isolated_db, tmp_path, monkeypatch):
    """Both taps funnel through ``_backlog_spawn``, so a manual dispatch gets the
    floor exactly as the loop's own spawn does — there is no second model-pick
    path to keep in sync."""
    import server.automations.model_pick as model_pick
    monkeypatch.setattr(model_pick, "assess_model", _assess_returning("haiku"))

    orch = _build_backlog_orch(
        {"max_concurrent": 1, "max_spawns_per_day": 10}, tier_floor="opus")
    _patch_git(monkeypatch, tmp_path)
    captured: dict = {}
    from server.automations.ownership import CanonicalTarget, owned
    from server.db import db_session
    from server.db.models import Automation, Session, DispatchOutcome, Project, ProjectRepo
    from unittest.mock import AsyncMock
    from sqlalchemy import select

    async with db_session() as db:
        db.add(Automation(id="backlog-runner", config={
            "max_concurrent": 1, "max_spawns_per_day": 10}))
        project = Project(slug="widget", name="widget", automation_policy={
            "dispatch": "auto", "default_model": "auto"})
        db.add(project)
        await db.flush()
        db.add(ProjectRepo(project_id=project.id, repo="o/r", is_primary=True))

    async def resolve_repos(names):
        return {name: name.casefold() for name in names}

    monkeypatch.setattr("server.automations.target_identity.resolve_repositories", resolve_repos)

    async def resolve(refs, *, kind):
        assert kind == "issue"
        return tuple(CanonicalTarget("I_" + ref.split("#")[1], ref) for ref in refs)

    monkeypatch.setattr("server.automations.target_identity.resolve_targets", resolve)
    # Explicit empty legacy population; the actual claim and state transaction run.
    monkeypatch.setattr("server.automations.ownership_reconciliation.reconcile_legacy_targets",
                        AsyncMock())

    async def launch(**kw):
        captured.update(kw)
        async with owned(kw["ownership_token"]) as tx:
            sid = tx.reserve_session()
            session = Session(id=sid, name="applied floor", cwd="/tmp", account_name="acct",
                              origin="backlog-runner", origin_ref="o/r#7", model=kw["model"])
            tx.db.add(session)
            tx.bind_session(sid)
        return session

    orch._backlog_ctx.spawn_session = launch  # type: ignore[method-assign]

    async def query_queue(config):
        return [{"ref": "o/r#7", "repo": "o/r", "title": "fix the frobnicator",
                 "product": "widget"}]

    async def noop(*a, **kw):
        return None

    async def account_for(ctx, ref):
        return "acct"

    orch._backlog_query_queue = query_queue  # type: ignore[method-assign]
    orch._backlog_announce = noop            # type: ignore[method-assign]
    orch._automation_account_for = account_for  # type: ignore[method-assign]
    orch._backlog_ctx.emit = noop                   # type: ignore[method-assign]

    from server.automations.backlog_runner import BacklogRunnerLoop

    orch._backlog_count_in_flight = AsyncMock(return_value=0)
    orch._backlog_loop = BacklogRunnerLoop(
        orch._backlog_ctx, select_batch=noop, spawn=orch._backlog_spawn,
        count_in_flight=orch._backlog_count_in_flight, healthy_account=noop,
        pr_status_for=noop, pr_doctor_active=noop, read_config=orch._backlog_read_config,
        admission=orch._backlog_admission, clock=lambda: 1)
    res = await orch.backlog_runner_dispatch(["o/r#7"])

    assert res["status"] == "spawned"
    assert captured["model"] == "opus"
    assert captured["detail"]["model_source"] == "auto+floor"
    assert captured["detail"]["assessed"] == "haiku"

    async with db_session() as db:
        outcome = await db.scalar(select(DispatchOutcome).where(
            DispatchOutcome.target_ref == "o/r#7"))
        assert outcome is not None and outcome.model == "opus"


# --------------------------------------------------------------------------- #
# backlog-runner model ladder at the spawn (spec 2026-09-18 §4)                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_backlog_spawn_pins_the_ladder_rung_over_an_auto_pick(tmp_path, monkeypatch):
    """Spec 2026-09-18 §4: a ``stronger_model`` hold makes the spawn run at the
    rung, even though the product says ``auto`` and the assessor would pick
    ``sonnet``. The ladder is the DECISION, not a floor under a fresh guess — so
    the rung is pinned (``model_source: pinned``) rather than handed to
    ``pick_model`` as a floor, which only ever raises an ``auto`` assessment."""
    import server.automations.model_pick as model_pick
    monkeypatch.setattr(model_pick, "assess_model", _assess_returning("sonnet"))

    orch = _build_backlog_orch({}, tier_floor=None, ladder_rung="opus")
    seen: dict = {}

    async def rung(product_slug, primary_ref, *, default_model, floor):
        seen.update(product=product_slug, ref=primary_ref,
                    default_model=default_model, floor=floor)
        return "opus"
    orch._backlog_ladder_rung = rung  # type: ignore[method-assign]
    _patch_git(monkeypatch, tmp_path)
    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    await orch._backlog_spawn(_BATCH, "acct", default_model="auto", ownership_token="test-token")

    # Asked for the PRIMARY issue, in the batch's product, with the floor the
    # step before it resolved (the rung never climbs below it).
    assert seen == {"product": "widget", "ref": "o/r#7",
                    "default_model": "auto", "floor": None}
    assert captured["model"] == "opus"
    assert captured["detail"] == {
        "issues": ["o/r#7"], "engine": "claude_code", "model": "opus",
        "model_source": "pinned", "ladder_rung": "opus",
        "dispatch_kind": "implement"}


@pytest.mark.asyncio
async def test_backlog_spawn_without_a_rung_is_unchanged(tmp_path, monkeypatch):
    """No hold, no rung: the ordinary ``auto`` pick stands and the spawn record
    says nothing about a ladder it is not on."""
    import server.automations.model_pick as model_pick
    monkeypatch.setattr(model_pick, "assess_model", _assess_returning("sonnet"))

    orch = _build_backlog_orch({}, tier_floor=None, ladder_rung=None)
    _patch_git(monkeypatch, tmp_path)
    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    await orch._backlog_spawn(_BATCH, "acct", default_model="auto", ownership_token="test-token")

    assert captured["model"] == "sonnet"
    assert "ladder_rung" not in captured["detail"]


@pytest.mark.asyncio
async def test_backlog_spawn_survives_a_failing_rung_lookup(tmp_path, monkeypatch):
    """§13, exactly as for the floor: a seam that RAISES costs the RUNG, never
    the spawn — the item keeps its hold and the next tick tries again."""
    import server.automations.model_pick as model_pick
    monkeypatch.setattr(model_pick, "assess_model", _assess_returning("sonnet"))

    orch = _build_backlog_orch({})

    async def boom(product_slug, primary_ref, *, default_model, floor):
        raise RuntimeError("ladder read down (injected)")
    orch._backlog_ladder_rung = boom  # type: ignore[method-assign]
    _patch_git(monkeypatch, tmp_path)
    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    await orch._backlog_spawn(_BATCH, "acct", default_model="auto", ownership_token="test-token")

    assert captured["model"] == "sonnet"
    assert "ladder_rung" not in captured["detail"]


@pytest.mark.asyncio
async def test_backlog_spawn_consumes_the_stronger_model_hold(isolated_db, tmp_path,
                                                              monkeypatch):
    """The dispatch the hold was asking for has happened, so the hold is spent
    (spec §4) — on every issue of the batch, not just the primary. Left in place
    it would re-park the item the moment the session ended."""
    from datetime import datetime, timezone

    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import AuditEvent, WorkItem
    from server.pm.holds import set_runner_stronger_hold

    now = datetime.now(timezone.utc)
    async with db_session() as db:
        for number in (7, 8):
            wi = WorkItem(repo="o/r", number=number, kind="issue", state="open",
                          title="t", body="b")
            set_runner_stronger_hold(wi, reason="bounced at sonnet", now=now)
            db.add(wi)

    batch = Batch(primary="o/r#7", issues=["o/r#7", "o/r#8"], repo="o/r",
                  titles={"o/r#7": "fix the frobnicator"}, product="widget")
    orch = _build_backlog_orch({}, ladder_rung="opus")
    _patch_git(monkeypatch, tmp_path)
    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    await orch._backlog_spawn(batch, "acct", default_model="auto", ownership_token="test-token")

    assert captured["model"] == "opus"
    async with db_session() as db:
        held = (await db.execute(select(WorkItem.hold_kind))).scalars().all()
        assert held == [None, None]
        rows = (await db.execute(select(AuditEvent).where(
            AuditEvent.type == "pm.hold.release"))).scalars().all()
        assert [r.detail["signal"] for r in rows] == ["dispatch", "dispatch"]
        assert all(r.identity == "runner" and r.via == "runner" for r in rows)


@pytest.mark.asyncio
async def test_backlog_spawn_without_a_rung_keeps_the_hold(isolated_db, tmp_path, monkeypatch):
    """Review F1: only a dispatch that actually CLIMBED consumes the hold. A spawn
    with no rung ran at the ordinary pick — the hold read failed, ``ladder_top``
    was lowered under the hold, or the engine has no ladder — so spending the hold
    would buy a re-run at the same tier and a bounce counted against
    ``give_up_after_bounces`` for nothing."""
    from datetime import datetime, timezone

    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import AuditEvent, WorkItem
    from server.pm.holds import set_runner_stronger_hold

    async with db_session() as db:
        wi = WorkItem(repo="o/r", number=7, kind="issue", state="open", title="t", body="b")
        set_runner_stronger_hold(wi, reason="bounced at sonnet",
                                 now=datetime.now(timezone.utc))
        db.add(wi)

    orch = _build_backlog_orch({}, ladder_rung=None)
    _patch_git(monkeypatch, tmp_path)
    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    await orch._backlog_spawn(_BATCH, "acct", default_model="auto", ownership_token="test-token")

    async with db_session() as db:
        assert (await db.execute(select(WorkItem.hold_kind))).scalars().all() == [
            "stronger_model"]
        assert (await db.execute(select(AuditEvent).where(
            AuditEvent.type == "pm.hold.release"))).scalars().all() == []


@pytest.mark.asyncio
async def test_backlog_spawn_investigates_an_evidence_held_primary(tmp_path, monkeypatch):
    """Spec 2026-09-18 §5: an evidence-held primary is an auto-filed error report
    nobody has triaged, so the dispatch is an INVESTIGATION — the investigate seed
    prompt, ``dispatch_kind="investigate"`` on the spawn record and on the value
    the loop reads back, and no ladder rung (an evidence item never climbs, so the
    rung seam's answer is never even asked for, let alone pinned)."""
    orch = _build_backlog_orch({}, ladder_rung="opus")
    _patch_git(monkeypatch, tmp_path)

    async def no_branch_listing(repo):
        raise AssertionError("an investigation must not resolve a default branch")
    # Review r1 F4: only the implement prompt takes a branch, and resolving one is
    # a remote branch listing — an investigation must not pay for it.
    orch._sdlc_default_branch = no_branch_listing  # type: ignore[method-assign]
    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    rec = await orch._backlog_spawn(_EVIDENCE_BATCH, "acct", default_model="sonnet",
                                    ownership_token="test-token")

    assert "Investigate; do not implement" in captured["initial_prompt"]
    assert captured["detail"]["dispatch_kind"] == "investigate"
    assert "ladder_rung" not in captured["detail"]
    assert captured["model"] == "sonnet"        # the stubbed rung never pinned "opus"
    assert rec.dispatch_kind == "investigate"


@pytest.mark.asyncio
async def test_backlog_spawn_without_an_evidence_hold_implements(tmp_path, monkeypatch):
    """The other side of the same switch: no evidence hold, so the batch is
    implemented exactly as before and the dispatch records itself as such."""
    orch = _build_backlog_orch({})
    _patch_git(monkeypatch, tmp_path)
    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    rec = await orch._backlog_spawn(_BATCH, "acct", default_model="sonnet",
                                    ownership_token="test-token")

    assert "Investigate; do not implement" not in captured["initial_prompt"]
    assert "Open ONE pull request" in captured["initial_prompt"]
    assert captured["detail"]["dispatch_kind"] == "implement"
    assert rec.dispatch_kind == "implement"


@pytest.mark.asyncio
async def test_backlog_spawn_carries_the_accepted_decision_verbatim(tmp_path, monkeypatch):
    """Spec 2026-09-18 §6: the decision the person took in the inbox reaches the
    next implement run WORD FOR WORD, and the seam is asked about the batch's
    issues — not just the primary, since a bundled issue can be the decided one."""
    from server.automations.backlog_prompt import DECISIONS_HEADING
    text = "Decision (Server-side): render it on the server because X."
    orch = _build_backlog_orch({}, accepted_decisions={"o/r#7": text})
    _patch_git(monkeypatch, tmp_path)
    asked: dict = {}

    async def decisions(refs):
        asked["refs"] = list(refs)
        return {"o/r#7": text}
    orch._backlog_accepted_decisions = decisions  # type: ignore[method-assign]
    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    await orch._backlog_spawn(_BATCH, "acct", default_model="sonnet",
                              ownership_token="test-token")

    assert asked["refs"] == _BATCH.issues
    assert DECISIONS_HEADING in captured["initial_prompt"]
    assert f"- o/r#7: {text}" in captured["initial_prompt"]


@pytest.mark.asyncio
async def test_backlog_spawn_leaves_an_undecided_prompt_alone(tmp_path, monkeypatch):
    """Nothing decided, nothing added: the overwhelming majority of dispatches
    must read exactly as they did before decisions existed."""
    from server.automations.backlog_prompt import DECISIONS_HEADING
    orch = _build_backlog_orch({})
    _patch_git(monkeypatch, tmp_path)
    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    await orch._backlog_spawn(_BATCH, "acct", default_model="sonnet",
                              ownership_token="test-token")

    assert DECISIONS_HEADING not in captured["initial_prompt"]


@pytest.mark.asyncio
async def test_backlog_spawn_investigation_never_reads_decisions(tmp_path, monkeypatch):
    """An investigation asks what the problem is; its four exits are its whole
    contract (spec §5), so it neither pays for the read nor carries the block."""
    from server.automations.backlog_prompt import DECISIONS_HEADING
    orch = _build_backlog_orch({})
    _patch_git(monkeypatch, tmp_path)

    async def boom(refs):
        raise AssertionError("an investigation must not read decisions")
    orch._backlog_accepted_decisions = boom  # type: ignore[method-assign]
    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    await orch._backlog_spawn(_EVIDENCE_BATCH, "acct", default_model="sonnet",
                              ownership_token="test-token")

    assert DECISIONS_HEADING not in captured["initial_prompt"]


@pytest.mark.asyncio
async def test_backlog_spawn_survives_a_failing_decisions_read(tmp_path, monkeypatch):
    """Best-effort (spec §13): a failed read costs the BLOCK, never the spawn —
    the dispatch runs exactly as it would have before anyone decided anything."""
    from server.automations.backlog_prompt import DECISIONS_HEADING
    orch = _build_backlog_orch({})
    _patch_git(monkeypatch, tmp_path)

    async def boom(refs):
        raise RuntimeError("db down")
    orch._backlog_accepted_decisions = boom  # type: ignore[method-assign]
    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    rec = await orch._backlog_spawn(_BATCH, "acct", default_model="sonnet",
                                    ownership_token="test-token")

    assert rec is not None
    assert DECISIONS_HEADING not in captured["initial_prompt"]
    assert "Open ONE pull request" in captured["initial_prompt"]


@pytest.mark.asyncio
async def test_backlog_spawn_of_a_batch_built_outside_the_queue_implements(
        tmp_path, monkeypatch):
    """A hold this runner never saw is not a kind: a batch reconstructed without a
    candidate row (``primary_hold_kind`` unset, the dataclass default) dispatches
    as the implementation the runner has always made, never as an investigation
    whose prompt tells the session not to fix anything."""
    orch = _build_backlog_orch({}, ladder_rung=None)
    _patch_git(monkeypatch, tmp_path)
    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    rec = await orch._backlog_spawn(
        Batch(primary="o/r#7", issues=["o/r#7"], repo="o/r"), "acct",
        default_model="sonnet", ownership_token="test-token")

    assert captured["detail"]["dispatch_kind"] == "implement"
    assert rec.dispatch_kind == "implement"


@pytest.mark.asyncio
async def test_backlog_spawn_that_dedupes_onto_an_existing_session_keeps_the_hold(
        isolated_db, tmp_path, monkeypatch):
    """Final review M1: the release is guarded on ``deduped`` exactly like the pick
    audit beside it. On the re-join path ``create_session`` hands back the session
    that already held the dedupe key — it is running at whatever model it was
    started with, NOT at the rung — so nothing climbed and the hold is not spent."""
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import AuditEvent, WorkItem
    from server.pm.holds import set_runner_stronger_hold

    async with db_session() as db:
        wi = WorkItem(repo="o/r", number=7, kind="issue", state="open", title="t", body="b")
        set_runner_stronger_hold(wi, reason="bounced at sonnet",
                                 now=datetime.now(timezone.utc))
        db.add(wi)

    orch = _build_backlog_orch({}, ladder_rung="opus")
    _patch_git(monkeypatch, tmp_path)
    existing = SimpleNamespace(id="already-running", model="sonnet")
    orch._backlog_ctx._existing_session = AsyncMock(return_value=existing)  # type: ignore[method-assign]

    async def dedupe(**_kw):
        return existing
    orch._backlog_ctx.spawn_session = dedupe  # type: ignore[method-assign]
    rec = await orch._backlog_spawn(_BATCH, "acct", default_model="auto",
                                    ownership_token="test-token")

    assert rec is existing
    async with db_session() as db:
        assert (await db.execute(select(WorkItem.hold_kind))).scalars().all() == [
            "stronger_model"]
        assert (await db.execute(select(AuditEvent).where(
            AuditEvent.type == "pm.hold.release"))).scalars().all() == []


@pytest.mark.asyncio
async def test_backlog_spawn_that_produces_no_session_keeps_the_hold(
        isolated_db, tmp_path, monkeypatch):
    """A refused spawn never dispatched anything, so the hold it was climbing
    for is NOT consumed: the claim is released and the next refill re-claims the
    item at the same rung. Releasing here would silently drop the ladder's
    memory of the bounce."""
    from datetime import datetime, timezone

    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import WorkItem
    from server.pm.holds import set_runner_stronger_hold

    async with db_session() as db:
        wi = WorkItem(repo="o/r", number=7, kind="issue", state="open", title="t", body="b")
        set_runner_stronger_hold(wi, reason="bounced at sonnet",
                                 now=datetime.now(timezone.utc))
        db.add(wi)

    orch = _build_backlog_orch({}, ladder_rung="opus")
    _patch_git(monkeypatch, tmp_path)

    async def refuse(**kw):
        return None
    orch._backlog_ctx.spawn_session = refuse  # type: ignore[method-assign]
    assert await orch._backlog_spawn(
        _BATCH, "acct", default_model="auto", ownership_token="test-token") is None

    async with db_session() as db:
        assert (await db.execute(select(WorkItem.hold_kind))).scalars().all() == [
            "stronger_model"]


# --------------------------------------------------------------------------- #
# backlog-runner Codex path (issue #1289)                                     #
# --------------------------------------------------------------------------- #

def _fake_launch_plan_codex(config):
    from server.sessions.engine import AgentEngine
    async def plan(origin_ref):
        return AgentEngine.CODEX, config
    return plan


@pytest.mark.asyncio
async def test_backlog_spawn_codex_auto_uses_codex_assessor(tmp_path, monkeypatch):
    orch = _build_backlog_orch({})
    _patch_git(monkeypatch, tmp_path)
    orch._backlog_ctx.launch_plan_or_refuse = _fake_launch_plan_codex(
        {"default_model": "auto"})

    seen: dict = {}
    async def fake_codex_effort(account, config, task_summary):
        seen["account"] = account
        seen["summary"] = task_summary
        return "high", "auto"
    orch._resolve_codex_auto_effort = fake_codex_effort  # type: ignore[method-assign]

    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    await orch._backlog_spawn(_BATCH, "acct", default_model="auto", ownership_token="test-token")

    assert seen["account"] == "acct"
    assert "fix the frobnicator" in seen["summary"]
    # A Codex spawn must never carry a Claude alias in `model=` — only the
    # live-catalog reasoning effort.
    assert captured["model"] is None
    assert captured["reasoning_effort"] == "high"
    assert captured["detail"] == {
        "issues": ["o/r#7"], "engine": "codex", "dispatch_kind": "implement",
        "reasoning_effort": "high", "model_source": "auto",
    }


@pytest.mark.asyncio
async def test_backlog_spawn_codex_assessment_failure_falls_back(tmp_path, monkeypatch):
    orch = _build_backlog_orch({})
    _patch_git(monkeypatch, tmp_path)
    orch._backlog_ctx.launch_plan_or_refuse = _fake_launch_plan_codex(
        {"default_model": "auto"})

    async def fake_codex_effort(account, config, task_summary):
        return None, None
    orch._resolve_codex_auto_effort = fake_codex_effort  # type: ignore[method-assign]

    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    await orch._backlog_spawn(_BATCH, "acct", default_model="auto", ownership_token="test-token")

    assert captured["model"] is None
    assert captured["reasoning_effort"] is None
    assert captured["detail"] == {"issues": ["o/r#7"], "engine": "codex",
                                  "dispatch_kind": "implement"}


# --------------------------------------------------------------------------- #
# SessionOrchestrator._resolve_codex_auto_effort (issue #1289)                #
# --------------------------------------------------------------------------- #

class _FakeCodexOption:
    def __init__(self, id_, default=False, reasoning_efforts=()):
        self.id = id_
        self.default = default
        self.reasoning_efforts = reasoning_efforts


class _FakeCodexAvailability:
    cli_version = "1.0.0"


class _FakeCodexManager:
    available = True
    availability = _FakeCodexAvailability()

    async def ensure(self, account):
        pass

    def client_for(self, account):
        return object()


class _FakeCodexCatalog:
    def __init__(self, options):
        self._options = options

    async def models(self, account, *, client, cli_version, refresh=False):
        return self._options


def _bare_orchestrator():
    import server.sessions.orchestrator as orch_mod
    return orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)


@pytest.mark.asyncio
async def test_resolve_codex_auto_effort_uses_pinned_model_catalog(monkeypatch):
    import server.automations.model_pick as model_pick
    orch = _bare_orchestrator()
    orch._codex = _FakeCodexManager()
    orch._codex_model_catalog = _FakeCodexCatalog((
        _FakeCodexOption("gpt-5.1-codex-mini", default=True,
                         reasoning_efforts=("minimal", "low")),
        _FakeCodexOption("gpt-5.1-codex", reasoning_efforts=("low", "medium", "high")),
    ))
    seen: dict = {}
    async def fake_resolve(config, task_summary, efforts):
        seen["efforts"] = efforts
        return "high", "auto"
    monkeypatch.setattr(model_pick, "resolve_codex_model", fake_resolve)

    got = await orch._resolve_codex_auto_effort(
        "acct", {"default_model": "auto", "codex_model": "gpt-5.1-codex"}, "hard job")
    assert seen["efforts"] == ("low", "medium", "high")
    assert got == ("high", "auto")


@pytest.mark.asyncio
async def test_resolve_codex_auto_effort_falls_back_to_catalog_default(monkeypatch):
    import server.automations.model_pick as model_pick
    orch = _bare_orchestrator()
    orch._codex = _FakeCodexManager()
    orch._codex_model_catalog = _FakeCodexCatalog((
        _FakeCodexOption("gpt-5.1-codex-mini", default=True,
                         reasoning_efforts=("minimal", "low")),
    ))
    seen: dict = {}
    async def fake_resolve(config, task_summary, efforts):
        seen["efforts"] = efforts
        return "minimal", "auto"
    monkeypatch.setattr(model_pick, "resolve_codex_model", fake_resolve)

    # No codex_model pin → falls back to the catalog's own default option.
    got = await orch._resolve_codex_auto_effort(
        "acct", {"default_model": "auto"}, "trivial job")
    assert seen["efforts"] == ("minimal", "low")
    assert got == ("minimal", "auto")


@pytest.mark.asyncio
async def test_resolve_codex_auto_effort_skips_when_not_auto():
    orch = _bare_orchestrator()
    orch._codex = _FakeCodexManager()  # never touched — short-circuits first
    got = await orch._resolve_codex_auto_effort(
        "acct", {"default_model": "default"}, "job")
    assert got == (None, None)


@pytest.mark.asyncio
async def test_resolve_codex_auto_effort_fails_open_without_codex_manager():
    orch = _bare_orchestrator()
    orch._codex = None
    got = await orch._resolve_codex_auto_effort(
        "acct", {"default_model": "auto"}, "job")
    assert got == (None, None)


@pytest.mark.asyncio
async def test_resolve_codex_auto_effort_fails_open_on_catalog_error():
    from server.codex.catalog import ModelCatalogError

    class _BoomCatalog:
        async def models(self, *a, **kw):
            raise ModelCatalogError("app-server unreachable")

    orch = _bare_orchestrator()
    orch._codex = _FakeCodexManager()
    orch._codex_model_catalog = _BoomCatalog()
    got = await orch._resolve_codex_auto_effort(
        "acct", {"default_model": "auto"}, "job")
    assert got == (None, None)


# --------------------------------------------------------------------------- #
# pr-doctor (_pr_doctor_session_fix fresh-spawn branch)                        #
# --------------------------------------------------------------------------- #

def _full_orch():
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1"}):
        return SessionOrchestrator(sdk_factory=fake_sdk_factory())


_INFO = PullRequestInfo(number=11, url="https://x/11", state="open",
                        checks="failing", title="red build", repo="o/r",
                        head_ref="fix-lint")


@pytest.mark.asyncio
async def test_pr_fix_spawn_passes_pinned_model(isolated_db, monkeypatch):
    from server.tests.test_pr_doctor_step import github_identity
    github_identity.__wrapped__(monkeypatch)
    from server.automations.registry import registry
    await registry.ensure_rows()
    orch = _full_orch()

    async def config():
        return {"default_model": "opus"}
    captured: dict = {}
    with patch("server.sessions.orchestrator.gitrepos.ensure_clone",
               return_value="/tmp/x"), \
         patch.object(orch, "_pr_doctor_config", config), \
         patch.object(orch._pr_doctor_ctx, "spawn_session", _capture_spawn(captured)):
        status, sid, _reason = await orch._pr_doctor_session_fix(
            "o/r", "fix-lint", _INFO, "failing_ci", None)
    assert (status, sid) == ("spawned", "new-sess")
    assert captured["model"] == "opus"
    assert captured["detail"] == {
        "engine": "claude_code", "model": "opus", "model_source": "pinned"}


@pytest.mark.asyncio
async def test_pr_fix_spawn_auto_fails_open_without_model_access(isolated_db, monkeypatch):
    from server.tests.test_pr_doctor_step import github_identity
    github_identity.__wrapped__(monkeypatch)
    from server.automations.registry import registry
    await registry.ensure_rows()
    # Default config resolves to "auto"; with no reachable assessor (the
    # conftest no-real-model guard = production's SDK-unavailable degraded
    # mode) the spawn proceeds on the account default instead of failing.
    orch = _full_orch()
    captured: dict = {}
    with patch("server.sessions.orchestrator.gitrepos.ensure_clone",
               return_value="/tmp/x"), \
         patch.object(orch._pr_doctor_ctx, "spawn_session", _capture_spawn(captured)):
        status, _, _reason = await orch._pr_doctor_session_fix(
            "o/r", "fix-lint", _INFO, "failing_ci", None)
    assert status == "spawned"
    assert captured["model"] is None
    # detail always carries the resolved engine now (Phase 7, #1267), even
    # when there is no model to report.
    assert captured["detail"] == {"engine": "claude_code"}


@pytest.mark.asyncio
async def test_backlog_spawn_that_dedupes_onto_an_existing_session_is_unaudited(
        isolated_db, tmp_path, monkeypatch):
    """``spawn_session`` hands back the EXISTING record on a dedupe hit (it only
    answers ``None`` for a refusal), so the audit must key on "a new session was
    created", not on "a record came back": a pick that landed on a session
    spawned earlier was never applied."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import server.automations.model_pick as model_pick
    monkeypatch.setattr(model_pick, "assess_model", _assess_returning("sonnet"))

    orch = _build_backlog_orch({}, tier_floor="opus")
    _patch_git(monkeypatch, tmp_path)
    existing = SimpleNamespace(id="already-running", model="sonnet")
    orch._backlog_ctx._existing_session = AsyncMock(return_value=existing)  # type: ignore[method-assign]

    async def dedupe(**kw):
        return existing
    orch._backlog_ctx.spawn_session = dedupe  # type: ignore[method-assign]
    rec = await orch._backlog_spawn(_BATCH, "acct", default_model="auto", ownership_token="test-token")
    assert rec is existing
    assert await _audit_model_picks() == []
