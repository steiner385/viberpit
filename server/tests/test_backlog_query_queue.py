"""The runner's candidate pool comes from the global priority queue (spec 003 §7a).

``SessionOrchestrator._backlog_query_queue`` is the ONE composition all three
runner consumers (selection, grouping refresh, the eligible/blocked counts) draw
from: ``rank(load_queue_rows(...))`` over the Projects v2 board mirror, with the
runner's own claims excluded and the ranking knobs read from stored config. No
GitHub call is involved — a real (SQLite) mirror is seeded and read.
"""
from __future__ import annotations

from server.automations.backlog_grouping import ParsedPlan

import asyncio

from server.db import db_session, init_db
from server.db.models import (DispatchOutcome, GhProject, GhProjectItem, Project, ProjectRepo,
                              WorkItem, WorkItemDep)
from server.pm.outcomes import body_fingerprint


def _orch_with_ctx():
    import server.sessions.orchestrator as orch_mod
    from server.automations.context import AutomationContext
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._backlog_ctx = AutomationContext("backlog-runner")
    orch._backlog_last_queue = []
    orch._backlog_last_policies = {}
    orch._backlog_last_repos = {}
    return orch


async def _init(tmp_path, name="q.db"):
    from server.automations.registry import registry
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / name}")
    await registry.ensure_rows()


async def _seed_product(slug: str, repo: str, *,
                        policy: dict | None = None) -> None:
    """A product the runner works: every candidate read is now scoped to products
    whose policy says ``dispatch != off``, so the fixture opts in by default."""
    async with db_session() as db:
        project = Project(slug=slug, name=slug,
                          automation_policy=policy or {"dispatch": "auto"})
        db.add(project)
        await db.flush()
        db.add(ProjectRepo(project_id=project.id, repo=repo, is_primary=True))
        db.add(GhProject(project_id=project.id, gh_node_id=f"PVT_{slug}",
                         gh_project_number=1, title=slug))
        await db.flush()


async def _set_policy(slug: str, policy: dict | None) -> None:
    from sqlalchemy import select
    async with db_session() as db:
        project = (await db.execute(select(Project).where(
            Project.slug == slug))).scalar_one()
        project.automation_policy = policy


async def _seed_item(repo: str, number: int, *, status: str = "Ready",
                     score: float | None = None,
                     hold_kind: str | None = None,
                     labels: list[str] | None = None) -> str:
    from sqlalchemy import select
    async with db_session() as db:
        item = WorkItem(repo=repo, number=number, kind="issue", state="open",
                        title=f"issue {number}", body="body",
                        url=f"https://github.com/{repo}/issues/{number}",
                        author="steiner385", labels=labels or [], assignees=[])
        if hold_kind is not None:
            item.hold_kind, item.hold_by, item.hold_reason = hold_kind, "tony@example.com", "max attempts"
        db.add(item)
        await db.flush()
        project_id = (await db.execute(select(ProjectRepo.project_id).where(
            ProjectRepo.repo == repo))).scalars().first()
        gh_id = (await db.execute(select(GhProject.id).where(
            GhProject.project_id == project_id))).scalars().first()
        db.add(GhProjectItem(gh_project_id=gh_id, work_item_id=item.id,
                             gh_item_node_id=f"PVTI_{repo}_{number}",
                             status=status, score=score))
        await db.flush()
        return item.id


def _config(**over) -> dict:
    cfg: dict = {}
    cfg.update(over)
    return cfg


# --------------------------------------------------------------------------- #
# Claims: an issue this runner already holds is not a candidate again          #
# --------------------------------------------------------------------------- #

def test_query_queue_excludes_refs_claimed_by_the_runner(tmp_path):
    async def run():
        await _init(tmp_path)
        await _seed_product("viberpit", "o/r")
        for n in (1, 2, 3, 4):
            await _seed_item("o/r", n, score=float(10 - n))
        orch = _orch_with_ctx()
        # #1 claimed with an explicit bundle (#1 + #2 rode one batch); #3 claimed
        # by a row that never recorded `issues` — the target itself is the claim.
        await orch._backlog_ctx.set_target_state(
            "o/r#1", {"claimed": True, "issues": ["o/r#1", "o/r#2"]})
        await orch._backlog_ctx.set_target_state("o/r#3", {"claimed": True})
        # A row that is NOT claimed must not hide its issue from the queue.
        await orch._backlog_ctx.set_target_state("o/r#4", {"claimed": False})

        cands = await orch._backlog_query_queue(_config())

        assert [c["ref"] for c in cands] == ["o/r#4"]

    asyncio.run(run())


def test_query_queue_records_the_last_ranked_result(tmp_path):
    """The ranked pool is kept for the status card's provenance (Phase C, Task 7)."""
    async def run():
        await _init(tmp_path, "last.db")
        await _seed_product("viberpit", "o/r")
        await _seed_item("o/r", 1, score=3.0)
        orch = _orch_with_ctx()

        cands = await orch._backlog_query_queue(_config())

        assert orch._backlog_last_queue == cands

    asyncio.run(run())


def test_held_item_is_skipped_not_a_claim(tmp_path):
    """Spec 2026-09-18 §3: a hold parks the item until a human signal releases
    it — there is no cooldown window, and the row is NOT a claim (that skip
    reason would be a lie about a live session)."""
    async def run():
        await _init(tmp_path, "held.db")
        await _seed_product("viberpit", "o/r",
                            policy={"dispatch": "auto", "queue_min_status": "Backlog"})
        await _seed_item("o/r", 5, status="Backlog", score=5.0, hold_kind="owner_action")
        orch = _orch_with_ctx()

        skipped: dict = {}
        cands = await orch._backlog_query_queue(_config(), skipped_out=skipped)
        assert cands == [] and skipped == {"held": 1}

    asyncio.run(run())


def test_gave_up_row_is_never_a_claim(tmp_path):
    """A ``gave_up`` row is not a claim (its session is long gone) — and since
    spec 2026-09-18 it is not a queue signal at all: the hold on the item is."""
    async def run():
        await _init(tmp_path, "gaveupnostamp.db")
        await _seed_product("viberpit", "o/r",
                            policy={"dispatch": "auto", "queue_min_status": "Backlog"})
        await _seed_item("o/r", 6, status="Backlog", score=5.0)
        orch = _orch_with_ctx()
        await orch._backlog_ctx.set_target_state(
            "o/r#6", {"repo": "o/r", "issues": ["o/r#6"], "gave_up": True,
                      "claimed": True})

        cands = await orch._backlog_query_queue(_config())

        assert [c["ref"] for c in cands] == ["o/r#6"]

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Counts: (eligible, blocked, untriaged) all come from the queue               #
# --------------------------------------------------------------------------- #

def test_count_candidates_returns_queue_eligible_and_blocked(tmp_path):
    async def run():
        await _init(tmp_path, "count.db")
        await _seed_product("viberpit", "o/r")
        await _seed_item("o/r", 1, score=5.0)          # eligible
        blocked = await _seed_item("o/r", 2, score=9.0)  # blocked by open #1
        async with db_session() as db:
            db.add(WorkItemDep(src_ref="o/r#2", dst_ref="o/r#1", source="native",
                               active=True, src_work_item_id=blocked))
        orch = _orch_with_ctx()

        async def _read_config():
            return "on", _config()
        orch._backlog_read_config = _read_config

        eligible, blocked, untriaged, _per_product = \
            await orch._backlog_count_candidates()
        assert (eligible, blocked, untriaged) == (1, 1, 0)

    asyncio.run(run())


def test_count_candidates_reports_below_min_status_items_as_untriaged(tmp_path):
    """In the default Ready mode, a board full of Backlog items would otherwise
    read as a bare "0 eligible" — an empty backlog rather than one waiting on
    triage. The status skip is its own gauge on the card (#990)."""
    async def run():
        await _init(tmp_path, "untriaged.db")
        await _seed_product("viberpit", "o/r")
        await _seed_item("o/r", 1, score=5.0)                     # Ready → eligible
        await _seed_item("o/r", 2, status="Backlog", score=9.0)   # untriaged
        await _seed_item("o/r", 3, status="Backlog", score=8.0)   # untriaged
        orch = _orch_with_ctx()

        async def _read_config():
            return "on", _config()
        orch._backlog_read_config = _read_config

        eligible, blocked, untriaged, _per_product = \
            await orch._backlog_count_candidates()
        assert (eligible, blocked, untriaged) == (1, 0, 2)

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Policy scoping: only products whose dispatch is on are in the pool           #
# --------------------------------------------------------------------------- #

def test_count_candidates_gauges_ignore_products_with_dispatch_off(tmp_path):
    """The card's gauges count the same pool selection draws from. Another
    product's stuck backlog must not read as "40 blocked" on a card whose runner
    was never turned on for it — the number is the "why isn't THIS backlog
    draining" note."""
    async def run():
        await _init(tmp_path, "blockedscope.db")
        await _seed_product("viberpit", "o/r")                     # dispatch on
        await _seed_product("kindash", "o/k", policy={"dispatch": "off"})
        await _seed_item("o/r", 1, score=1.0)                      # on, eligible
        await _seed_item("o/k", 2, status="Blocked", score=9.0)    # off, blocked
        orch = _orch_with_ctx()

        async def _read_config():
            return "on", _config()
        orch._backlog_read_config = _read_config

        eligible, blocked, untriaged, per_product = \
            await orch._backlog_count_candidates()
        assert (eligible, blocked, untriaged) == (1, 0, 0)
        assert set(per_product) == {"viberpit", "kindash"}
        assert per_product["kindash"]["eligible"] == 0
        assert per_product["kindash"]["blocked"] == 0

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# A failed queue read costs a spawn, never the whole tick                      #
# --------------------------------------------------------------------------- #

def test_select_batch_survives_a_failing_queue_read(tmp_path, monkeypatch):
    """The selection half of a tick shares its coroutine with the sweep half
    (reconcile / orphan heal / give-up), so a raising pool read would skip all of
    it. A queue read that blows up is "no batch this tick", exactly as the old
    label search returning [] was."""
    async def run():
        await _init(tmp_path, "selectfail.db")
        orch = _orch_with_ctx()

        async def _boom(config, *, skipped_out=None, products_out=None):
            raise RuntimeError("mirror unavailable")
        orch._backlog_query_queue = _boom

        async def _repo_load():
            return {}
        orch._backlog_repo_load = _repo_load
        monkeypatch.setattr("ccswitch.gitrepos.stale_credential_repos", lambda: [])

        assert await orch._backlog_select_batch(_config()) is None

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# The Ready-vs-Backlog choice is the PRODUCT's, and it reaches rank()          #
# --------------------------------------------------------------------------- #

def test_query_queue_min_status_comes_from_the_product_policy(tmp_path):
    async def run():
        await _init(tmp_path, "minstatus.db")
        await _seed_product("viberpit", "o/r")
        await _seed_item("o/r", 1, status="Ready", score=1.0)
        await _seed_item("o/r", 2, status="Backlog", score=9.0)
        orch = _orch_with_ctx()

        ready_only = await orch._backlog_query_queue(_config())
        assert [c["ref"] for c in ready_only] == ["o/r#1"]

        await _set_policy("viberpit", {"dispatch": "auto",
                                       "queue_min_status": "Backlog"})
        both = await orch._backlog_query_queue(_config())
        assert [c["ref"] for c in both] == ["o/r#2", "o/r#1"]

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# D8: a body that keeps bouncing is parked, not re-dispatched                  #
# --------------------------------------------------------------------------- #

def test_query_queue_excludes_a_bounced_row_and_reports_it(tmp_path):
    async def run():
        await _init(tmp_path, "bounced.db")
        await _seed_product("viberpit", "o/r",
                            policy={"dispatch": "auto", "give_up_after_bounces": 2})
        await _seed_item("o/r", 1, score=5.0)
        fp = body_fingerprint("issue 1", "body")
        async with db_session() as db:
            db.add_all([
                DispatchOutcome(target_ref="o/r#1", body_fingerprint=fp, outcome="no_pr"),
                DispatchOutcome(target_ref="o/r#1", body_fingerprint=fp, outcome="no_pr"),
            ])
        orch = _orch_with_ctx()

        per_product: dict = {}
        cands = await orch._backlog_query_queue(_config(), products_out=per_product)

        assert cands == []
        assert per_product["viberpit"]["bounced"] == 1

    asyncio.run(run())


def test_query_queue_keeps_a_bounced_row_when_give_up_after_bounces_is_zero(tmp_path):
    async def run():
        await _init(tmp_path, "neverbounce.db")
        await _seed_product("viberpit", "o/r",
                            policy={"dispatch": "auto", "give_up_after_bounces": 0})
        await _seed_item("o/r", 1, score=5.0)
        fp = body_fingerprint("issue 1", "body")
        async with db_session() as db:
            db.add_all([
                DispatchOutcome(target_ref="o/r#1", body_fingerprint=fp, outcome="no_pr"),
                DispatchOutcome(target_ref="o/r#1", body_fingerprint=fp, outcome="no_pr"),
            ])
        orch = _orch_with_ctx()

        per_product: dict = {}
        cands = await orch._backlog_query_queue(_config(), products_out=per_product)

        assert [c["ref"] for c in cands] == ["o/r#1"]
        assert per_product["viberpit"]["bounced"] == 0

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# The grouping refresh draws from the same pool (no label search)              #
# --------------------------------------------------------------------------- #

def test_refresh_groups_groups_the_queue_pool(tmp_path, monkeypatch):
    async def run():
        await _init(tmp_path, "groups.db")
        await _seed_product("viberpit", "o/r")
        await _seed_item("o/r", 1, score=5.0)
        orch = _orch_with_ctx()

        async def _healthy():
            return None
        orch._backlog_healthy_account = _healthy
        seen: dict = {}

        async def _propose(cands, config, _config_dir=None):
            seen["candidates"] = cands
            return ParsedPlan(groups=[{"refs": [c["ref"] for c in cands], "repo": "o/r"}])

        async def _store(groups, cands, *, degraded=False):
            seen["stored"] = cands
            return len(groups)

        monkeypatch.setattr("server.automations.backlog_grouping.propose_groups", _propose)
        monkeypatch.setattr("server.automations.backlog_grouping.store_plan", _store)

        assert (await orch._backlog_refresh_groups(_config())).groups == 1
        assert [c["ref"] for c in seen["candidates"]] == ["o/r#1"]

    asyncio.run(run())


def test_refresh_groups_runs_on_a_verified_helper_identity(tmp_path, monkeypatch):
    """Quota-healthy is not helper-ready (#1845/#1846): the grouping call used the
    healthy account's config dir unverified, so an org-denied account answered
    every refresh with the opaque "error result: success". The dir now comes from
    ``_autopilot_helper_config_dir`` (the verified helper identity), and with no
    healthy account from the helper picker rather than the daemon's empty env."""
    async def run():
        await _init(tmp_path, "groups-identity.db")
        await _seed_product("viberpit", "o/r")
        await _seed_item("o/r", 1, score=5.0)
        orch = _orch_with_ctx()
        healthy: list = ["acct-a"]

        async def _healthy():
            return healthy[0]
        orch._backlog_healthy_account = _healthy
        orch._autopilot_helper_config_dir = lambda acct: f"/verified/{acct}"
        monkeypatch.setattr("server.sessions.helper_call.pick_helper_config_dir",
                            lambda: "/picked")
        dirs: list = []

        async def _propose(cands, config, _config_dir=None):
            dirs.append(_config_dir)
            return ParsedPlan(groups=[])

        async def _store(groups, cands, *, degraded=False):
            return len(groups)

        monkeypatch.setattr("server.automations.backlog_grouping.propose_groups", _propose)
        monkeypatch.setattr("server.automations.backlog_grouping.store_plan", _store)

        await orch._backlog_refresh_groups(_config())
        healthy[0] = None
        await orch._backlog_refresh_groups(_config())
        assert dirs == ["/verified/acct-a", "/picked"]

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Evidence dispatch: family on the candidate, evidence out of the model's pool  #
# --------------------------------------------------------------------------- #

TRIAGE_REPO = "steiner385/viberpit"  # error-triage's schema default target repo


async def _seed_signature(number: int | None, logger: str, event: str) -> None:
    from server.db.models import ErrorSignature
    async with db_session() as db:
        db.add(ErrorSignature(fingerprint=f"fp{logger}{number}", source="edge",
                              level="ERROR", logger=logger, event=event,
                              issue_number=number))
        await db.flush()


def test_query_queue_attaches_the_family_to_evidence_candidates(tmp_path):
    """Sibling auto-error reports carry the same family across the hand-off; an
    ordinary candidate carries no family key at all."""
    async def run():
        await _init(tmp_path, "family.db")
        await _seed_product("viberpit", TRIAGE_REPO)
        await _seed_item(TRIAGE_REPO, 101, score=9.0, hold_kind="evidence")
        await _seed_item(TRIAGE_REPO, 102, score=8.0, hold_kind="evidence")
        await _seed_item(TRIAGE_REPO, 103, score=7.0)
        await _seed_signature(101, "server.a", "boom")
        await _seed_signature(102, "server.a", "boom")
        orch = _orch_with_ctx()

        cands = await orch._backlog_query_queue(_config())

        by_ref = {c["ref"]: c for c in cands}
        assert by_ref[f"{TRIAGE_REPO}#101"]["family"] == "server.a:boom"
        assert by_ref[f"{TRIAGE_REPO}#102"]["family"] == "server.a:boom"
        assert "family" not in by_ref[f"{TRIAGE_REPO}#103"]

    asyncio.run(run())


def test_query_queue_leaves_an_unmatched_evidence_candidate_familyless(tmp_path):
    async def run():
        await _init(tmp_path, "nofamily.db")
        await _seed_product("viberpit", TRIAGE_REPO)
        await _seed_item(TRIAGE_REPO, 101, score=9.0, hold_kind="evidence")
        orch = _orch_with_ctx()

        cands = await orch._backlog_query_queue(_config())

        assert cands[0]["family"] is None

    asyncio.run(run())


def test_refresh_groups_keeps_evidence_out_of_the_model_pool(tmp_path, monkeypatch):
    """An evidence item never shares a batch with implement work: its family group
    is built here and ranked into the plan, and the model only ever sees the rest.
    Here the family outscores the implement issue, so it also leads."""
    async def run():
        await _init(tmp_path, "evgroups.db")
        await _seed_product("viberpit", TRIAGE_REPO)
        await _seed_item(TRIAGE_REPO, 101, score=9.0, hold_kind="evidence")
        await _seed_item(TRIAGE_REPO, 102, score=8.0, hold_kind="evidence")
        await _seed_item(TRIAGE_REPO, 103, score=7.0)
        await _seed_signature(101, "server.a", "boom")
        await _seed_signature(102, "server.a", "boom")
        orch = _orch_with_ctx()

        async def _healthy():
            return None
        orch._backlog_healthy_account = _healthy
        seen: dict = {}

        async def _propose(cands, config, _config_dir=None):
            seen["proposed"] = [c["ref"] for c in cands]
            return ParsedPlan(groups=[{"refs": [c["ref"] for c in cands], "rationale": "model"}])

        async def _store(groups, cands, *, degraded=False):
            seen["stored"] = groups
            seen["pool"] = [c["ref"] for c in cands]
            return len(groups)

        monkeypatch.setattr("server.automations.backlog_grouping.propose_groups", _propose)
        monkeypatch.setattr("server.automations.backlog_grouping.store_plan", _store)

        assert (await orch._backlog_refresh_groups(_config(max_issues_per_pr=2))).groups == 2
        assert seen["proposed"] == [f"{TRIAGE_REPO}#103"]
        assert [g["refs"] for g in seen["stored"]] == [
            [f"{TRIAGE_REPO}#101", f"{TRIAGE_REPO}#102"],
            [f"{TRIAGE_REPO}#103"],
        ]
        # store_plan still receives the WHOLE pool, so every stored ref resolves
        # its repo/title.
        assert sorted(seen["pool"]) == sorted(
            [f"{TRIAGE_REPO}#{n}" for n in (101, 102, 103)])

    asyncio.run(run())


def test_refresh_groups_stores_an_evidence_only_plan_without_a_model_call(tmp_path,
                                                                          monkeypatch):
    """Nothing left for the model to partition must not mean a refresh failure —
    the old code raised when a plan came back empty over a non-empty pool."""
    async def run():
        await _init(tmp_path, "evonly.db")
        await _seed_product("viberpit", TRIAGE_REPO)
        await _seed_item(TRIAGE_REPO, 101, score=9.0, hold_kind="evidence")
        orch = _orch_with_ctx()

        async def _healthy():  # pragma: no cover - must not be reached
            raise AssertionError("no model call for an evidence-only pool")

        orch._backlog_healthy_account = _healthy
        seen: dict = {}

        async def _propose(cands, config, _config_dir=None):  # pragma: no cover
            raise AssertionError("no model call for an evidence-only pool")

        async def _store(groups, cands, *, degraded=False):
            seen["stored"] = groups
            return len(groups)

        monkeypatch.setattr("server.automations.backlog_grouping.propose_groups", _propose)
        monkeypatch.setattr("server.automations.backlog_grouping.store_plan", _store)

        assert (await orch._backlog_refresh_groups(_config(max_issues_per_pr=2))).groups == 1
        assert [g["refs"] for g in seen["stored"]] == [[f"{TRIAGE_REPO}#101"]]

    asyncio.run(run())


def test_refresh_groups_float_an_urgent_group_ahead_of_the_evidence_half(tmp_path,
                                                                         monkeypatch):
    """An incident must never land behind the day's auto-error reports — the exact
    shape #1698's urgent float exists to prevent. Two independent guards now hold
    it: the queue ranks urgency first (so the rank interleave alone puts it here)
    and the float is re-applied over the whole assembled plan. This asserts the
    outcome; ``test_backlog_grouping`` pins the float itself against a rank that
    disagrees."""
    async def run():
        await _init(tmp_path, "evurgent.db")
        await _seed_product("viberpit", TRIAGE_REPO)
        # The evidence reports outscore the incident, so only urgency can float it.
        await _seed_item(TRIAGE_REPO, 101, score=9.0, hold_kind="evidence")
        await _seed_item(TRIAGE_REPO, 102, score=8.0, hold_kind="evidence")
        await _seed_item(TRIAGE_REPO, 900, score=1.0, labels=["deploy-monitor"])
        await _seed_signature(101, "server.a", "boom")
        await _seed_signature(102, "server.a", "boom")
        orch = _orch_with_ctx()

        async def _healthy():
            return None
        orch._backlog_healthy_account = _healthy
        seen: dict = {}

        async def _propose(cands, config, _config_dir=None):
            return ParsedPlan(groups=[{"refs": [c["ref"] for c in cands], "rationale": "model"}])

        async def _store(groups, cands, *, degraded=False):
            seen["stored"] = groups
            return len(groups)

        monkeypatch.setattr("server.automations.backlog_grouping.propose_groups", _propose)
        monkeypatch.setattr("server.automations.backlog_grouping.store_plan", _store)

        assert (await orch._backlog_refresh_groups(_config(max_issues_per_pr=2))).groups == 2
        assert [g["refs"] for g in seen["stored"]] == [
            [f"{TRIAGE_REPO}#900"],                          # the incident, first
            [f"{TRIAGE_REPO}#101", f"{TRIAGE_REPO}#102"],    # the family, behind it
        ]

    asyncio.run(run())


def test_refresh_groups_ranks_the_evidence_half_among_the_model_half(tmp_path,
                                                                     monkeypatch):
    """Spec §5 dispatches an evidence hold as an ordinary candidate — same
    ranking. Concatenating the halves made a higher-scored, Ready implement issue
    wait behind every auto-filed error report (~25 of them in the §10 audit, one
    dispatch cycle each at the default cap). The two halves interleave by queue
    rank instead."""
    async def run():
        await _init(tmp_path, "evrank.db")
        await _seed_product("viberpit", TRIAGE_REPO)
        await _seed_item(TRIAGE_REPO, 103, score=9.0)                      # rank 0
        await _seed_item(TRIAGE_REPO, 101, score=8.0, hold_kind="evidence")  # rank 1
        await _seed_item(TRIAGE_REPO, 102, score=7.0, hold_kind="evidence")  # rank 2
        await _seed_signature(101, "server.a", "boom")
        await _seed_signature(102, "server.a", "boom")
        orch = _orch_with_ctx()

        async def _healthy():
            return None
        orch._backlog_healthy_account = _healthy
        seen: dict = {}

        async def _propose(cands, config, _config_dir=None):
            return ParsedPlan(groups=[{"refs": [c["ref"] for c in cands], "rationale": "model"}])

        async def _store(groups, cands, *, degraded=False):
            seen["stored"] = groups
            return len(groups)

        monkeypatch.setattr("server.automations.backlog_grouping.propose_groups", _propose)
        monkeypatch.setattr("server.automations.backlog_grouping.store_plan", _store)

        assert (await orch._backlog_refresh_groups(_config(max_issues_per_pr=2))).groups == 2
        assert [g["refs"] for g in seen["stored"]] == [
            [f"{TRIAGE_REPO}#103"],                          # the score-9 issue, first
            [f"{TRIAGE_REPO}#101", f"{TRIAGE_REPO}#102"],    # the family, behind it
        ]

    asyncio.run(run())


def test_refresh_groups_still_refreshes_evidence_when_the_model_call_fails(tmp_path,
                                                                           monkeypatch):
    """#1963: a ``GroupingModelError`` must not also discard the deterministic
    evidence half, which needs no model call and cannot itself have failed — a
    family whose primary converted mid-plan would otherwise stay stale (dispatching
    the wrong prompt, see ``test_backlog_grouping``) for a full grouping interval.
    The previous plan's non-evidence groups are carried over verbatim, and the
    caller still sees the raise so its existing backoff fires."""
    async def run():
        from server.automations.backlog_grouping import GroupingModelError
        await _init(tmp_path, "evfail.db")
        await _seed_product("viberpit", TRIAGE_REPO)
        await _seed_item(TRIAGE_REPO, 101, score=9.0, hold_kind="evidence")
        await _seed_item(TRIAGE_REPO, 103, score=7.0)
        await _seed_signature(101, "server.a", "boom")
        orch = _orch_with_ctx()

        async def _healthy():
            return None
        orch._backlog_healthy_account = _healthy

        async def _propose(cands, config, _config_dir=None):
            raise GroupingModelError("SDK transport failed")

        async def _load_plan():
            return [{"id": 9, "refs": [f"{TRIAGE_REPO}#900"], "rationale": "stale"},
                    {"id": 10, "refs": [f"{TRIAGE_REPO}#101"], "rationale": "old evidence"}]

        seen: dict = {}

        async def _store(groups, cands, *, degraded=False):
            seen["stored"] = groups
            seen["degraded"] = degraded
            return len(groups)

        monkeypatch.setattr("server.automations.backlog_grouping.propose_groups", _propose)
        monkeypatch.setattr("server.automations.backlog_grouping.load_plan", _load_plan)
        monkeypatch.setattr("server.automations.backlog_grouping.store_plan", _store)

        raised = False
        try:
            await orch._backlog_refresh_groups(_config())
        except GroupingModelError:
            raised = True
        assert raised, "the caller's backoff relies on seeing the failure"
        # The fresh evidence group for #101 replaces the stale plan's stand-in for
        # it; the unrelated #900 group (not a live candidate at all any more)
        # carries over verbatim rather than being lost.
        assert [g["refs"] for g in seen["stored"]] == [
            [f"{TRIAGE_REPO}#101"], [f"{TRIAGE_REPO}#900"]]
        assert seen["degraded"] is True

    asyncio.run(run())


def test_query_queue_survives_a_family_lookup_that_raises(tmp_path, monkeypatch):
    """Fail OPEN: the family read is an annotation, never the pool's life. A raising
    lookup leaves the evidence candidates familyless — which groups each alone, the
    pre-family behaviour — instead of costing the runner the tick."""
    async def run():
        await _init(tmp_path, "famboom.db")
        await _seed_product("viberpit", TRIAGE_REPO)
        await _seed_item(TRIAGE_REPO, 101, score=9.0, hold_kind="evidence")
        await _seed_item(TRIAGE_REPO, 103, score=7.0)
        await _seed_signature(101, "server.a", "boom")
        orch = _orch_with_ctx()

        def _boom(*_a, **_kw):
            raise RuntimeError("signature read failed")

        monkeypatch.setattr("server.pm.error_family.families_for_refs", _boom)

        cands = await orch._backlog_query_queue(_config())

        assert [c["ref"] for c in cands] == [f"{TRIAGE_REPO}#101", f"{TRIAGE_REPO}#103"]
        assert cands[0]["family"] is None

    asyncio.run(run())


def test_refresh_groups_carries_a_degraded_reply_to_the_store_and_the_summary(
        tmp_path, monkeypatch):
    """An unparseable model reply is stored as a plan (the runner needs SOMETHING
    to pop) but flagged all the way out — onto the generation, so the page can
    say so, and into the summary, so the loop emits a warning instead of a
    routine "refreshed" (#2217)."""
    async def run():
        await _init(tmp_path, "groups_degraded.db")
        await _seed_product("viberpit", "o/r")
        await _seed_item("o/r", 1, score=5.0)
        orch = _orch_with_ctx()

        async def _healthy():
            return None
        orch._backlog_healthy_account = _healthy
        seen: dict = {}

        async def _propose(cands, config, _config_dir=None):
            return ParsedPlan(groups=[{"refs": [c["ref"]], "rationale": None} for c in cands],
                              degraded=True)

        async def _store(groups, cands, *, degraded=False):
            seen["degraded"] = degraded
            return len(groups)

        monkeypatch.setattr("server.automations.backlog_grouping.propose_groups", _propose)
        monkeypatch.setattr("server.automations.backlog_grouping.store_plan", _store)

        summary = await orch._backlog_refresh_groups(_config())
        assert (summary.groups, summary.degraded) == (1, True)
        assert seen["degraded"] is True

    asyncio.run(run())


def test_query_queue_reports_held_and_the_full_skip_map_per_product(tmp_path):
    """Spec 2026-09-30 §4.1: the per-product row carries every skip reason, not
    three of nine, and ``held`` is its own key because it is what the escalation
    read model and the card's tile both read."""
    async def run():
        await _init(tmp_path, "heldmap.db")
        await _seed_product("viberpit", "o/r")
        await _seed_item("o/r", 1, score=1.0)                              # eligible
        await _seed_item("o/r", 2, score=2.0, hold_kind="owner_action")    # held
        await _seed_item("o/r", 3, score=3.0, hold_kind="decision")        # held
        await _seed_item("o/r", 4, status="Backlog", score=4.0)            # untriaged
        orch = _orch_with_ctx()
        per_product: dict = {}
        await orch._backlog_query_queue(_config(), products_out=per_product)
        row = per_product["viberpit"]
        assert row["eligible"] == 1
        assert row["held"] == 2
        assert row["skipped"] == {"held": 2, "status": 1}
        assert row["untriaged"] == 1  # the existing flat keys are unchanged

    asyncio.run(run())


def test_count_candidates_default_row_has_held_and_skipped_for_an_off_product(tmp_path):
    async def run():
        await _init(tmp_path, "heldoff.db")
        await _seed_product("viberpit", "o/r")
        await _seed_product("kindash", "o/k", policy={"dispatch": "off"})
        await _seed_item("o/r", 1, score=1.0)
        orch = _orch_with_ctx()

        async def _read_config():
            return "on", _config()
        orch._backlog_read_config = _read_config

        _e, _b, _u, per_product = await orch._backlog_count_candidates()
        assert per_product["kindash"]["held"] == 0
        assert per_product["kindash"]["skipped"] == {}

    asyncio.run(run())


def test_load_queue_rows_carries_the_hold_provenance(tmp_path):
    """Spec 2026-09-30 §3: the read model needs who parked the item, why and when,
    so the loader threads the three hold columns beside ``hold_kind``."""
    async def run():
        await _init(tmp_path, "holdprov.db")
        await _seed_product("viberpit", "o/r")
        await _seed_item("o/r", 1, score=1.0, hold_kind="owner_action")
        from datetime import datetime, timezone

        from server.db import db_session
        from server.pm.queue_store import load_queue_rows

        async with db_session() as db:
            rows = await db.run_sync(lambda s: load_queue_rows(
                s, now=datetime.now(timezone.utc), claimed_refs=set()))
        (row,) = rows
        assert row.hold_kind == "owner_action"
        assert row.hold_by == "tony@example.com"
        assert row.hold_reason == "max attempts"
        # ``_seed_item`` does not stamp hold_set_at; the field must still exist.
        assert row.hold_set_at is None

    asyncio.run(run())
