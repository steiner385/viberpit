"""The runner's orchestrator seams read PRODUCT policies (spec §5, phase 1).

``Project.automation_policy`` — not the runner's retired global repo knobs — now says
which products the Backlog Runner works, how far down each board it reaches, each
product's in-flight cap/weight and its attempt cap/model. These tests drive the four
seams that translate policies into the shapes the pure selection code already takes,
plus the one-shot seed that carries the old ``enabled_repos`` settings across the flip.

A real (SQLite) mirror is seeded and read — no GitHub call is involved.
"""
from __future__ import annotations

from server.tests.test_backlog_runner_loop import (
    isolated_ownership_identity as isolated_ownership_identity,
)

import asyncio

from server.db import db_session, init_db
from server.db.models import (AuditEvent, AutomationEvent, GhProject, GhProjectItem,
                              Project, ProjectRepo, WorkItem)


def _orch_with_ctx():
    import server.sessions.orchestrator as orch_mod
    from server.automations.context import AutomationContext
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._backlog_ctx = AutomationContext("backlog-runner")
    orch._pr_doctor_ctx = AutomationContext("pr-doctor")
    orch._backlog_last_queue = []
    orch._backlog_last_policies = {}
    orch._backlog_last_repos = {}
    return orch


async def _init(tmp_path, name="policy_seams.db"):
    from server.automations.registry import registry
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / name}")
    await registry.ensure_rows()


async def _seed_product(slug: str, repo: str, *, policy: dict | None = None,
                        board: bool = True) -> None:
    async with db_session() as db:
        project = Project(slug=slug, name=slug, automation_policy=policy)
        db.add(project)
        await db.flush()
        db.add(ProjectRepo(project_id=project.id, repo=repo, is_primary=True))
        if board:
            db.add(GhProject(project_id=project.id, gh_node_id=f"PVT_{slug}",
                             gh_project_number=1, title=slug))
        await db.flush()


async def _seed_item(repo: str, number: int, *, status: str = "Ready",
                     score: float | None = None, created=None) -> None:
    from sqlalchemy import select
    async with db_session() as db:
        item = WorkItem(repo=repo, number=number, kind="issue", state="open",
                        title=f"issue {number}", body="body",
                        url=f"https://github.com/{repo}/issues/{number}",
                        author="steiner385", labels=[], assignees=[],
                        gh_created_at=created)
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


def _config(**over) -> dict:
    cfg = {"selection_strategy": "balanced", "needs_human_retry_days": 7}
    cfg.update(over)
    return cfg


async def _set_runner_row(*, enabled=True, mode="auto", config=None) -> None:
    from sqlalchemy import select
    from server.db.models import Automation
    async with db_session() as db:
        row = (await db.execute(select(Automation).where(
            Automation.id == "backlog-runner"))).scalar_one()
        row.enabled = enabled
        row.mode = mode
        row.config = config or {}


# --------------------------------------------------------------------------- #
# 1. Modes: a stored propose/auto row reads as the new global ``on``           #
# --------------------------------------------------------------------------- #

def test_stored_propose_or_auto_mode_folds_to_on(tmp_path):
    """The global vocabulary is ``off | on`` now (propose-vs-auto moved to the
    product). Rows written before the flip still carry ``propose``/``auto``, and
    they must keep running — the fold lives in ``_backlog_read_config``."""
    async def run():
        await _init(tmp_path, "fold.db")
        orch = _orch_with_ctx()

        await _set_runner_row(mode="auto")
        assert (await orch._backlog_read_config())[0] == "on"
        await _set_runner_row(mode="propose")
        assert (await orch._backlog_read_config())[0] == "on"
        await _set_runner_row(mode="on")
        assert (await orch._backlog_read_config())[0] == "on"
        await _set_runner_row(enabled=False, mode="auto")
        assert (await orch._backlog_read_config())[0] == "off"

    asyncio.run(run())


def test_a_stored_off_mode_stays_off(tmp_path):
    """The pre-flip UI rendered a three-option select including ``off``, so a row
    an operator PARKED there while leaving the enable switch on reads ``off``.
    ``read_automation_config`` answers ``default_mode`` for any mode it was not
    given, so dropping ``off`` from ``modes`` would restart that row on the next
    tick — it has to survive the read verbatim."""
    async def run():
        await _init(tmp_path, "storedoff.db")
        orch = _orch_with_ctx()
        await _set_runner_row(enabled=True, mode="off")
        assert (await orch._backlog_read_config())[0] == "off"

    asyncio.run(run())


def test_an_enabled_row_with_no_mode_runs(tmp_path):
    """With exactly two modes the UI hides the mode select, so its toggle enables
    the row with ``mode`` still NULL — without a ``default_mode`` an enabled
    runner would read ``off`` forever (the same trap every other two-mode
    automation passes ``default_mode`` to avoid)."""
    async def run():
        await _init(tmp_path, "nomode.db")
        orch = _orch_with_ctx()
        await _set_runner_row(enabled=True, mode=None)
        assert (await orch._backlog_read_config())[0] == "on"
        await _set_runner_row(enabled=False, mode=None)
        assert (await orch._backlog_read_config())[0] == "off"

    asyncio.run(run())


def test_read_config_drops_the_retired_repo_knobs_but_keeps_the_rest(tmp_path):
    async def run():
        await _init(tmp_path, "retired.db")
        orch = _orch_with_ctx()
        await _set_runner_row(mode="auto", config={
            "enabled_repos": ["o/r"], "repo_weights": {"o/r": 3},
            "repo_caps": {"o/r": 2}, "queue_min_status": "Backlog",
            "max_attempts": 5, "default_model": "opus",
            "respect_dependencies": False, "max_concurrent": 6})
        mode, config = await orch._backlog_read_config()
        assert mode == "on"
        assert config["max_concurrent"] == 6          # kept settings survive
        for retired in ("enabled_repos", "repo_weights", "repo_caps",
                        "queue_min_status", "max_attempts", "default_model",
                        "respect_dependencies"):
            assert retired not in config

    asyncio.run(run())


def test_read_raw_config_returns_the_unvalidated_blob_and_stored_mode(tmp_path):
    """The seed needs what validation throws away — the retired values, the
    pre-fold mode and ``enabled`` (which the validated read collapses into
    ``off``) — so it reads the row a second time, unvalidated."""
    async def run():
        await _init(tmp_path, "raw.db")
        orch = _orch_with_ctx()
        await _set_runner_row(mode="auto", config={
            "enabled_repos": ["o/r"], "repo_caps": {"o/r": 2}, "max_concurrent": 6})
        enabled, mode_before, raw = await orch._backlog_read_raw_config()
        assert (enabled, mode_before) == (True, "auto")
        assert raw["enabled_repos"] == ["o/r"]
        assert raw["repo_caps"] == {"o/r": 2}

        # A row the registry created and nobody ever configured: disabled, mode
        # NULL. Distinguishable from a row an operator turned on.
        await _set_runner_row(enabled=False, mode=None, config={})
        assert await orch._backlog_read_raw_config() == (False, None, {})

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# 3. Queue scoping + per-product ranking                                       #
# --------------------------------------------------------------------------- #

def test_query_queue_scopes_to_products_whose_dispatch_is_on(tmp_path):
    """``effective_policy(...)["dispatch"] != "off"`` is the ONE "which products
    are on" reading — a product that never opted in contributes nothing, and its
    skips never land in this runner's gauges."""
    async def run():
        await _init(tmp_path, "scope.db")
        await _seed_product("on-product", "o/r", policy={"dispatch": "auto"})
        await _seed_product("off-product", "o/k")        # no policy → off
        await _seed_item("o/r", 1, score=1.0)
        await _seed_item("o/k", 2, score=9.0)            # higher score, still out
        orch = _orch_with_ctx()

        skipped: dict = {}
        cands = await orch._backlog_query_queue(_config(), skipped_out=skipped)

        assert [c["ref"] for c in cands] == ["o/r#1"]
        assert skipped == {}

    asyncio.run(run())


def test_query_queue_ranks_each_product_by_its_own_min_status(tmp_path):
    """Board reach is per product now: a Backlog-reaching product yields its
    untriaged items while a Ready-only product does not — one rank() call per
    product, merged by score."""
    async def run():
        await _init(tmp_path, "minstatus.db")
        await _seed_product("deep", "o/d",
                            policy={"dispatch": "auto", "queue_min_status": "Backlog"})
        await _seed_product("shallow", "o/s",
                            policy={"dispatch": "auto", "queue_min_status": "Ready"})
        await _seed_item("o/d", 1, status="Backlog", score=5.0)
        await _seed_item("o/s", 2, status="Backlog", score=9.0)   # below its reach
        await _seed_item("o/s", 3, status="Ready", score=1.0)
        orch = _orch_with_ctx()

        skipped: dict = {}
        cands = await orch._backlog_query_queue(_config(), skipped_out=skipped)

        assert [c["ref"] for c in cands] == ["o/d#1", "o/s#3"]     # score desc
        assert skipped == {"status": 1}

    asyncio.run(run())


def test_query_queue_publishes_per_product_counts_and_caches_policies(tmp_path):
    async def run():
        await _init(tmp_path, "perproduct.db")
        await _seed_product("alpha", "o/a",
                            policy={"dispatch": "propose", "max_in_flight": 3})
        await _seed_product("beta", "o/b")                  # off
        await _seed_item("o/a", 1, score=5.0)
        await _seed_item("o/a", 2, status="Backlog", score=4.0)   # untriaged
        await _seed_item("o/b", 3, score=9.0)
        orch = _orch_with_ctx()

        products: dict = {}
        await orch._backlog_query_queue(_config(), products_out=products)

        assert set(products) == {"alpha"}           # only ON products are reported
        assert products["alpha"]["dispatch"] == "propose"
        assert products["alpha"]["eligible"] == 1
        assert products["alpha"]["untriaged"] == 1
        # The same read feeds the other seams in this tick.
        assert orch._backlog_last_policies["alpha"]["max_in_flight"] == 3
        assert orch._backlog_last_repos["alpha"] == ["o/a"]

    asyncio.run(run())


def test_query_queue_breaks_cross_product_ties_by_issue_age(tmp_path):
    """The runner ranks one product at a time (each has its own reach) and merges.
    That merge sorts by ``queue.rank_key`` — ``rank``'s OWN key — not a copy of its
    first two legs: re-sorting on score alone breaks every cross-product tie by
    product slug (the append order), which on a board with no priority scores yet
    reorders the entire pool and stops handing the oldest issue out first."""
    from datetime import datetime

    async def run():
        await _init(tmp_path, "tiebreak.db")
        # "zeta" sorts AFTER "alpha", so slug order and age order disagree.
        await _seed_product("alpha", "o/a", policy={"dispatch": "auto"})
        await _seed_product("zeta", "o/z", policy={"dispatch": "auto"})
        await _seed_item("o/a", 1, score=5.0, created=datetime(2026, 5, 1))
        await _seed_item("o/z", 2, score=5.0, created=datetime(2026, 1, 1))
        # …and the same again with no scores at all, the common board state.
        await _seed_item("o/a", 3, created=datetime(2026, 6, 1))
        await _seed_item("o/z", 4, created=datetime(2026, 2, 1))
        orch = _orch_with_ctx()

        cands = await orch._backlog_query_queue(_config())

        assert [c["ref"] for c in cands] == ["o/z#2", "o/a#1", "o/z#4", "o/a#3"]

    asyncio.run(run())


def test_count_candidates_returns_product_policies_without_rereading_claims(tmp_path):
    from unittest.mock import AsyncMock

    async def run():
        await _init(tmp_path, "counts.db")
        await _seed_product("alpha", "o/a",
                            policy={"dispatch": "auto", "max_in_flight": 2})
        await _seed_product("off", "o/off",
                            policy={"dispatch": "off", "max_in_flight": 3})
        await _seed_item("o/a", 1, score=5.0)
        await _seed_item("o/a", 2, score=4.0)
        orch = _orch_with_ctx()

        async def _read_config():
            return "on", _config()
        orch._backlog_read_config = _read_config
        # One live claim, tagged with the product that owns it.
        await orch._backlog_ctx.set_target_state(
            "o/a#9", {"claimed": True, "issues": ["o/a#9"], "product": "alpha"})

        orch._backlog_ctx.list_target_states = AsyncMock(
            side_effect=AssertionError("display claims must use loop snapshot"))
        eligible, blocked, untriaged, per_product = \
            await orch._backlog_count_candidates()

        assert (eligible, blocked, untriaged) == (2, 0, 0)
        assert "in_flight" not in per_product["alpha"]
        assert per_product["alpha"]["cap"] == 2
        assert per_product["off"]["dispatch"] == "off"
        assert per_product["off"]["cap"] == 3
        assert per_product["off"]["eligible"] == 0
        orch._backlog_ctx.list_target_states.assert_not_awaited()

    asyncio.run(run())


def test_the_global_and_per_product_in_flight_ledgers_count_the_same_rows(tmp_path):
    """ONE predicate (``is_in_flight``) behind both slot ledgers. Two filters over
    the same rows is how a product reads "2/2 in flight" while the fleet-wide
    ``max_concurrent`` gate counts 3 — neither number wrong on its own terms, and
    no way to tell from the card which one the loop acted on."""
    async def run():
        from server.automations.backlog_runner import in_flight_by_product
        await _init(tmp_path, "ledgers.db")
        orch = _orch_with_ctx()
        rows = {
            "o/a#1": {"claimed": True, "product": "alpha"},                # spawned
            "o/a#2": {"claimed": True, "proposed": True, "product": "alpha"},  # pending tap
            "o/a#3": {"claimed": True, "product": "alpha", "gave_up": True},   # freed
            "o/b#4": {"claimed": True, "product": "beta", "handed_off": True},  # PR Doctor's
            "o/b#5": {"claimed": True, "product": "beta"},
            "o/c#6": {"claimed": True},                       # pre-flip claim, no product
        }
        for target, state in rows.items():
            await orch._backlog_ctx.set_target_state(target, state)

        states = await orch._backlog_ctx.list_target_states()
        per_product = in_flight_by_product(states)
        global_count = await orch._backlog_count_in_flight()

        assert per_product == {"alpha": 2, "beta": 1}
        # The ONLY difference is the productless row: it holds a fleet slot but
        # there is no product to charge it to.
        assert global_count == sum(per_product.values()) + 1

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# 4. Selection: policies → the repo-keyed weights / caps / order                #
# --------------------------------------------------------------------------- #

def test_select_batch_translates_policies_into_repo_keyed_knobs(tmp_path, monkeypatch):
    """``select_batch``/``plan_to_batch`` stay repo-keyed and unchanged; this seam
    is the ONE place a product policy becomes a per-repo weight/order.

    ``max_in_flight`` is NOT among them: it is a PRODUCT ceiling, enforced per
    product in the loop's ``_refill``. Aliased onto each of a product's repos it
    let a product owning N repos run N × its cap, and only under the one strategy
    that reads caps at all."""
    async def run():
        await _init(tmp_path, "translate.db")
        await _seed_product("alpha", "o/a",
                            policy={"dispatch": "auto", "weight": 3, "max_in_flight": 2})
        await _seed_product("beta", "o/b",
                            policy={"dispatch": "propose", "weight": 1,
                                    "max_in_flight": 5})
        await _seed_product("gamma", "o/g")                  # off → absent
        await _seed_item("o/a", 1, score=5.0)
        orch = _orch_with_ctx()

        async def _repo_load():
            return {}
        orch._backlog_repo_load = _repo_load
        # Patched through the orchestrator's own module reference (the one it
        # calls ``gitrepos.stale_credential_repos()`` on) so the legacy package
        # name is never spelled here (brand ratchet, #1374).
        from server.sessions import orchestrator as _orch_mod
        monkeypatch.setattr(_orch_mod.gitrepos, "stale_credential_repos", lambda: [])

        seen: dict = {}

        async def _fake_select_batch(config, **kwargs):
            seen.update(kwargs)
            seen["pool"] = await kwargs["query"](config)
            return None

        monkeypatch.setattr("server.automations.backlog_runner.select_batch",
                            _fake_select_batch)

        assert await orch._backlog_select_batch(_config()) is None
        assert seen["weights"] == {"o/a": 3, "o/b": 1}
        assert "caps" not in seen                      # a PRODUCT ceiling, not a repo one
        assert seen["repo_order"] == ["o/a", "o/b"]     # Project.created_at order
        assert [c["ref"] for c in seen["pool"]] == ["o/a#1"]

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# 2. The one-shot seed (D10)                                                   #
# --------------------------------------------------------------------------- #

async def _policies() -> dict:
    from sqlalchemy import select
    async with db_session() as db:
        rows = (await db.execute(select(Project.slug, Project.automation_policy,
                                        Project.automation_policy_updated_at))).all()
    return {slug: (policy, stamp) for slug, policy, stamp in rows}


def test_seed_writes_policies_for_the_owning_products_of_enabled_repos(tmp_path):
    async def run():
        await _init(tmp_path, "seed.db")
        await _seed_product("alpha", "o/a")
        await _seed_product("beta", "o/b")
        await _seed_product("gamma", "o/g")          # not in enabled_repos
        orch = _orch_with_ctx()

        raw = {"enabled_repos": ["o/a", "o/b"], "repo_caps": {"o/a": 4},
               "repo_weights": {"o/a": 3}, "queue_min_status": "Backlog",
               "max_attempts": 5, "default_model": "opus",
               "respect_dependencies": False}
        assert await orch._backlog_seed_policies(raw, "auto") == 2

        policies = await _policies()
        alpha, stamp = policies["alpha"]
        # ``triage_auto_accept`` and ``give_up_after_bounces`` are stored at their
        # defaults: the legacy runner config never had a triage knob or a bounce
        # threshold, so the seed cannot invent a setting for either.
        assert alpha == {"dispatch": "auto", "queue_min_status": "Backlog",
                         "max_in_flight": 4, "weight": 3, "max_attempts": 5,
                         "give_up_after_bounces": 2, "default_model": "opus",
                         "respect_dependencies": False, "triage_auto_accept": "off"}
        assert stamp is not None
        beta, _ = policies["beta"]
        assert beta["max_in_flight"] == 1 and beta["weight"] == 1   # unlisted → 1
        assert policies["gamma"] == (None, None)

        # Audited per product with the runner's identity, and announced ONCE.
        from sqlalchemy import select
        async with db_session() as db:
            audits = (await db.execute(select(AuditEvent).where(
                AuditEvent.type == "pm.project.policy"))).scalars().all()
            events = (await db.execute(select(AutomationEvent).where(
                AutomationEvent.kind == "policies_seeded"))).scalars().all()
        assert {a.identity for a in audits} == {"backlog-runner"}
        assert {a.detail["slug"] for a in audits} == {"alpha", "beta"}
        assert all(a.detail["seeded_from"] == "enabled_repos" for a in audits)
        assert len(events) == 1
        assert sorted(events[0].detail["products"]) == ["alpha", "beta"]

    asyncio.run(run())


def test_seed_maps_a_propose_mode_row_to_propose_dispatch(tmp_path):
    async def run():
        await _init(tmp_path, "seedpropose.db")
        await _seed_product("alpha", "o/a")
        orch = _orch_with_ctx()
        assert await orch._backlog_seed_policies({"enabled_repos": ["o/a"]},
                                                 "propose") == 1
        assert (await _policies())["alpha"][0]["dispatch"] == "propose"

    asyncio.run(run())


def test_seed_with_no_enabled_repos_covers_every_product_with_a_board(tmp_path):
    """An empty ``enabled_repos`` meant "every onboarded product" — the seed has
    to preserve exactly that reach, or the flip would silently stop work."""
    async def run():
        await _init(tmp_path, "seedall.db")
        await _seed_product("alpha", "o/a")
        await _seed_product("beta", "o/b")
        await _seed_product("boardless", "o/n", board=False)
        orch = _orch_with_ctx()

        assert await orch._backlog_seed_policies({"enabled_repos": []}, "auto") == 2

        policies = await _policies()
        assert policies["alpha"][0]["dispatch"] == "auto"
        assert policies["beta"][0]["dispatch"] == "auto"
        assert policies["boardless"] == (None, None)

    asyncio.run(run())


def test_a_hand_set_policy_survives_the_seed_without_blocking_the_others(tmp_path):
    """Per-PRODUCT skip, not a global gate: a human who set a policy before the
    first tick owns THAT product's answer, and the seed must not overwrite it with
    the retired defaults — but their edit is no reason to strand every other
    product on the wrong side of the migration."""
    async def run():
        await _init(tmp_path, "seedonce.db")
        await _seed_product("alpha", "o/a", policy={"dispatch": "off"})
        await _seed_product("beta", "o/b")
        orch = _orch_with_ctx()

        assert await orch._backlog_seed_policies({"enabled_repos": ["o/a", "o/b"]},
                                                 "auto") == 1
        policies = await _policies()
        assert policies["alpha"][0] == {"dispatch": "off"}    # untouched
        assert policies["beta"][0]["dispatch"] == "auto"      # migrated

    asyncio.run(run())


def test_the_seed_consumes_the_retired_keys_and_the_legacy_mode_off_the_row(tmp_path):
    """The DURABLE one-shot marker (M1/M4): the seed strips every retired key from
    the runner row's own config in the SAME session it writes the policies, and
    folds a pre-flip ``propose``/``auto`` mode to the phase-1 ``on`` the UI offers.
    Settings that are still schema fields are untouched."""
    async def run():
        from sqlalchemy import select
        from server.automations.backlog_runner import BacklogRunner
        from server.db.models import Automation
        await _init(tmp_path, "seedmarker.db")
        await _seed_product("alpha", "o/a")
        orch = _orch_with_ctx()
        await _set_runner_row(enabled=True, mode="auto", config={
            "enabled_repos": ["o/a"], "repo_caps": {"o/a": 4},
            "queue_min_status": "Backlog", "max_attempts": 5,
            "default_model": "opus", "respect_dependencies": False,
            "max_concurrent": 6})

        assert await orch._backlog_seed_policies_once() == 1

        async with db_session() as db:
            row = (await db.execute(select(Automation).where(
                Automation.id == "backlog-runner"))).scalar_one()
            config, mode = dict(row.config or {}), row.mode
        assert not set(config) & set(BacklogRunner.retired_config_keys)
        assert config["max_concurrent"] == 6          # a live field is left alone
        assert mode == "on"

    asyncio.run(run())


def test_the_marker_is_consumed_even_when_no_product_is_seeded(tmp_path):
    """A legacy row on an install with nothing to seed (no onboarded products yet)
    is still MIGRATED: leaving the retired keys behind would re-read them on a
    later start and opt products in that were onboarded after the flip, at the old
    global settings nobody chose for them."""
    async def run():
        from sqlalchemy import select
        from server.automations.backlog_runner import BacklogRunner
        from server.db.models import Automation
        await _init(tmp_path, "seednoproducts.db")
        orch = _orch_with_ctx()
        await _set_runner_row(enabled=True, mode="propose",
                              config={"enabled_repos": ["o/a"]})

        assert await orch._backlog_seed_policies_once() == 0

        async with db_session() as db:
            row = (await db.execute(select(Automation).where(
                Automation.id == "backlog-runner"))).scalar_one()
            assert not set(row.config or {}) & set(BacklogRunner.retired_config_keys)
            assert row.mode == "on"

    asyncio.run(run())


def test_clearing_every_policy_does_not_re_arm_the_seed(tmp_path):
    """The bug the marker fixes: the old gate ("does any product have a policy")
    re-armed the moment an operator cleared the last one, so the next process
    start silently re-opted every product back in at the retired defaults. The
    marker lives on the runner ROW, which nothing in the policy UI touches."""
    async def run():
        from server.api.pm import PolicyPatchBody, update_project_policy
        await _init(tmp_path, "rearm.db")
        await _seed_product("alpha", "o/a")
        await _seed_product("beta", "o/b")
        orch = _orch_with_ctx()
        await _set_runner_row(enabled=True, mode="auto",
                              config={"enabled_repos": ["o/a", "o/b"]})

        assert await orch._backlog_seed_policies_once() == 2

        # Cleared through the real PATCH handler — ``{"policy": null}`` writes SQL
        # NULL (the column is ``JSON(none_as_null=True)``), which is exactly the
        # state the old Python scan read as "never seeded".
        async with db_session() as db:
            for slug in ("alpha", "beta"):
                await update_project_policy(slug, PolicyPatchBody(policy=None),
                                            identity="tester", db=db)
        assert await _policies() == {"alpha": (None, None), "beta": (None, None)}

        assert await orch._backlog_seed_policies_once() == 0
        assert await _policies() == {"alpha": (None, None), "beta": (None, None)}

    asyncio.run(run())


def test_seed_takes_the_primary_repos_cap_else_the_lowest_named_repos(tmp_path):
    """The retired caps/weights were keyed by REPO; a product's PRIMARY repo is the
    one that carried its settings. A product that never flagged a primary falls
    back to its lowest-named repo rather than silently inheriting nothing."""
    async def run():
        from sqlalchemy import select
        await _init(tmp_path, "seedprimary.db")
        await _seed_product("alpha", "o/z")            # o/z is alpha's primary
        await _seed_product("beta", "o/m")
        async with db_session() as db:
            # alpha owns a second, non-primary repo; beta's only repo stops being
            # primary and gains a lower-named sibling.
            alpha_id = (await db.execute(select(Project.id).where(
                Project.slug == "alpha"))).scalars().one()
            beta_id = (await db.execute(select(Project.id).where(
                Project.slug == "beta"))).scalars().one()
            db.add(ProjectRepo(project_id=alpha_id, repo="o/a", is_primary=False))
            db.add(ProjectRepo(project_id=beta_id, repo="o/b", is_primary=False))
            beta_repo = (await db.execute(select(ProjectRepo).where(
                ProjectRepo.repo == "o/m"))).scalars().one()
            beta_repo.is_primary = False
        orch = _orch_with_ctx()

        assert await orch._backlog_seed_policies(
            {"enabled_repos": [], "repo_caps": {"o/z": 7, "o/a": 3,
                                                "o/b": 5, "o/m": 9}}, "auto") == 2

        policies = await _policies()
        assert policies["alpha"][0]["max_in_flight"] == 7      # the primary's
        assert policies["beta"][0]["max_in_flight"] == 5       # lowest-named o/b

    asyncio.run(run())


def test_seed_leaves_a_key_the_legacy_config_never_set_at_the_schema_default(tmp_path):
    """Only keys the old config ACTUALLY set are carried over. A repo absent from
    ``repo_caps`` was UNCAPPED; writing a literal in its place would invent a
    ceiling nobody chose, so the schema default stands instead."""
    async def run():
        await _init(tmp_path, "seeddefaults.db")
        await _seed_product("alpha", "o/a")
        orch = _orch_with_ctx()

        # A garbage stored value is "never set" too — it must not be coerced.
        assert await orch._backlog_seed_policies(
            {"enabled_repos": ["o/a"], "repo_caps": {"o/a": "lots"},
             "max_attempts": None}, "auto") == 1

        from server.automations.product_policy import effective_policy
        defaults = effective_policy(None)
        alpha, _ = (await _policies())["alpha"]
        assert alpha["max_in_flight"] == defaults["max_in_flight"]
        assert alpha["max_attempts"] == defaults["max_attempts"]
        assert alpha["queue_min_status"] == defaults["queue_min_status"]
        assert alpha["respect_dependencies"] == defaults["respect_dependencies"]
        assert alpha["default_model"] == defaults["default_model"]

    asyncio.run(run())


# --- the seed's gate: an enabled row that carries a stored mode ------------- #

def test_seed_once_is_a_no_op_on_a_never_configured_runner_row(tmp_path):
    """The registry creates a ``backlog-runner`` row on every install. On one where
    nobody ever turned the runner on, that row is disabled with ``mode`` NULL and
    there is nothing to migrate — seeding from it would opt EVERY product in at
    ``propose``, overwriting the schema's deliberate ``dispatch: off`` default."""
    async def run():
        await _init(tmp_path, "seedvirgin.db")
        await _seed_product("alpha", "o/a")
        orch = _orch_with_ctx()
        await _set_runner_row(enabled=False, mode=None, config={})

        assert await orch._backlog_seed_policies_once() == 0
        assert (await _policies())["alpha"] == (None, None)

    asyncio.run(run())


def test_seed_once_is_a_no_op_while_the_runner_row_is_disabled(tmp_path):
    """A runner a human turned OFF is not running; its products' policies are the
    operator's to set when they turn it back on."""
    async def run():
        await _init(tmp_path, "seeddisabled.db")
        await _seed_product("alpha", "o/a")
        orch = _orch_with_ctx()
        await _set_runner_row(enabled=False, mode="auto",
                              config={"enabled_repos": ["o/a"]})

        assert await orch._backlog_seed_policies_once() == 0
        assert (await _policies())["alpha"] == (None, None)

    asyncio.run(run())


def test_seed_once_is_a_no_op_on_a_row_carrying_no_retired_key(tmp_path):
    """The durable gate: a config with none of the runner's retired keys has
    already been migrated (or never carried legacy settings), so there is nothing
    to seed from — regardless of what any product's policy currently says."""
    async def run():
        await _init(tmp_path, "seedmigrated.db")
        await _seed_product("alpha", "o/a")
        orch = _orch_with_ctx()
        await _set_runner_row(enabled=True, mode="on", config={"max_concurrent": 3})

        assert await orch._backlog_seed_policies_once() == 0
        assert (await _policies())["alpha"] == (None, None)

    asyncio.run(run())


def test_seed_once_runs_for_an_enabled_row_with_a_stored_mode(tmp_path):
    """The case the migration exists for: a runner that WAS working, whose global
    mode becomes each of its products' ``dispatch``."""
    async def run():
        await _init(tmp_path, "seedlive.db")
        await _seed_product("alpha", "o/a")
        orch = _orch_with_ctx()
        await _set_runner_row(enabled=True, mode="auto",
                              config={"enabled_repos": ["o/a"]})

        assert await orch._backlog_seed_policies_once() == 1
        assert (await _policies())["alpha"][0]["dispatch"] == "auto"

    asyncio.run(run())


def test_seed_degrades_one_out_of_range_legacy_value_not_the_whole_policy(tmp_path):
    """A legacy value below one of the phase-1 minimums (``max_attempts: 0`` was
    legal pre-flip; so was a negative per-repo cap) must cost that ONE field its
    legacy value — never make ``effective_policy`` raise and reset the product to
    an all-defaults policy with ``dispatch: off``, which would be the migration
    silently stopping a product that was running while still consuming the marker."""
    async def run():
        await _init(tmp_path, "seedbelowmin.db")
        await _seed_product("alpha", "o/a")
        orch = _orch_with_ctx()
        await _set_runner_row(enabled=True, mode="auto", config={
            "enabled_repos": ["o/a"], "repo_caps": {"o/a": -1},
            "repo_weights": {"o/a": 4}, "max_attempts": 0})

        assert await orch._backlog_seed_policies_once() == 1
        policy = (await _policies())["alpha"][0]
        assert policy["dispatch"] == "auto"
        assert policy["weight"] == 4                    # the in-range value survives
        assert policy["max_attempts"] == 2              # schema default, not 0
        assert policy["max_in_flight"] == 1             # schema default, not -1

    asyncio.run(run())


def test_seed_once_normalises_a_legacy_mode_even_after_a_post_flip_config_save(tmp_path):
    """A runner-card config save strips the retired keys too (the config PATCH
    validates against the phase-1 schema), which consumes the marker without the
    seed having run. Products are NOT seeded from it — the ``enabled_repos`` list
    is gone, and seeding every board at the legacy mode would opt in products nobody
    chose — but the row's legacy ``auto`` must still fold to ``on`` so
    ``GET /api/automations`` stops describing a mode the runner no longer has."""
    async def run():
        from sqlalchemy import select
        from server.db.models import Automation
        await _init(tmp_path, "seedlegacymode.db")
        await _seed_product("alpha", "o/a")
        orch = _orch_with_ctx()
        await _set_runner_row(enabled=True, mode="auto", config={"max_concurrent": 3})

        assert await orch._backlog_seed_policies_once() == 0
        assert (await _policies())["alpha"] == (None, None)
        async with db_session() as db:
            row = (await db.execute(select(Automation).where(
                Automation.id == "backlog-runner"))).scalar_one()
            assert row.mode == "on"

    asyncio.run(run())


def test_pr_doctor_attempts_seam_reads_the_total_off_the_doctors_row(tmp_path):
    """``first_pass_merge`` hangs off this seam (spec §6, R3-4/R3-7): it answers
    how much recovery PR Doctor injected into a PR — session fixes AND mechanical
    ones — and ``None`` whenever that is UNKNOWN (a target it never tracked, or
    one whose row it already cleared at terminal).

    Two halves, both load-bearing:
    * UNKNOWN must never collapse to 0 — that would brand every late-swept merge
      a first-pass merge;
    * a TRACKED row the doctor never had to act on IS a real zero, even though
      neither counter exists yet: the counters only ever appear at 1 (see
      ``pr_doctor_engine.record_attempt``), so keying "unknown" on a missing
      ``total_attempts`` made ``first_pass_merge`` unreachable in production."""
    async def run():
        await _init(tmp_path, "prdoc_attempts.db")
        orch = _orch_with_ctx()

        # The monotonic, re-arm-immune totals — not the per-head ``attempts``.
        await orch._pr_doctor_ctx.set_target_state(
            "o/r#12", {"total_attempts": 2, "attempts": 1})
        assert await orch._backlog_pr_doctor_attempts_for("o/r#12") == 2

        # A tracked PR PR Doctor never had to act on: a real, known zero.
        await orch._pr_doctor_ctx.set_target_state(
            "o/r#13", {"total_attempts": 0, "last_head_sha": "abc"})
        assert await orch._backlog_pr_doctor_attempts_for("o/r#13") == 0

        # Tracked, never acted on, and the counters were never written at all
        # (the production shape of a clean PR) — still a known zero.
        await orch._pr_doctor_ctx.set_target_state("o/r#15", {"last_head_sha": "x"})
        assert await orch._backlog_pr_doctor_attempts_for("o/r#15") == 0

        # Either counter alone counts: first-pass means NO doctor recovery of any
        # kind, so a merge carried home by mechanical-only fixes is not one.
        await orch._pr_doctor_ctx.set_target_state("o/r#16", {"total_attempts": 1})
        assert await orch._backlog_pr_doctor_attempts_for("o/r#16") == 1
        await orch._pr_doctor_ctx.set_target_state("o/r#17", {"total_mechanical": 2})
        assert await orch._backlog_pr_doctor_attempts_for("o/r#17") == 2
        await orch._pr_doctor_ctx.set_target_state(
            "o/r#18", {"total_attempts": 1, "total_mechanical": 2})
        assert await orch._backlog_pr_doctor_attempts_for("o/r#18") == 3

        # No row at all → unknown.
        assert await orch._backlog_pr_doctor_attempts_for("o/r#404") is None

        # A CLEARED row (``set_target_state(target, {})`` at terminal) → unknown.
        await orch._pr_doctor_ctx.set_target_state("o/r#14", {})
        assert await orch._backlog_pr_doctor_attempts_for("o/r#14") is None

    asyncio.run(run())


def test_the_seam_counts_the_lifetime_totals_that_outlive_a_recovery_clear(tmp_path):
    """R3-10: the doctor's ``total_*`` ceiling is deliberately reset when CI
    recovers (a green PR earns a fresh budget), so ``total_*`` alone cannot answer
    "did the doctor ever act on this PR". The recovery clear carries the counts
    forward under ``lifetime_*``, which the ENGINE never reads — the seam sums all
    four, so a doctored PR reads ≥1 no matter how many recoveries it went through.
    An empty row is still UNKNOWN: the carry writes ``{}`` when there is nothing
    to carry, so ``{}`` still means "the doctor's row says nothing"."""
    async def run():
        await _init(tmp_path, "prdoc_lifetime.db")
        orch = _orch_with_ctx()

        # Post-recovery-clear shape: only the lifetime counters remain.
        await orch._pr_doctor_ctx.set_target_state(
            "o/r#20", {"lifetime_attempts": 2, "lifetime_mechanical": 0})
        assert await orch._backlog_pr_doctor_attempts_for("o/r#20") == 2
        await orch._pr_doctor_ctx.set_target_state(
            "o/r#21", {"lifetime_attempts": 0, "lifetime_mechanical": 1})
        assert await orch._backlog_pr_doctor_attempts_for("o/r#21") == 1

        # Recovered once, then doctored again: both halves count, never double.
        await orch._pr_doctor_ctx.set_target_state("o/r#22", {
            "lifetime_attempts": 2, "lifetime_mechanical": 1,
            "total_attempts": 1, "total_mechanical": 1, "attempts": 0})
        assert await orch._backlog_pr_doctor_attempts_for("o/r#22") == 5

        # A row carrying explicit zeros is still a known zero, not unknown.
        await orch._pr_doctor_ctx.set_target_state(
            "o/r#23", {"lifetime_attempts": 0, "lifetime_mechanical": 0,
                       "last_head_sha": "abc"})
        assert await orch._backlog_pr_doctor_attempts_for("o/r#23") == 0

    asyncio.run(run())


def test_the_recovery_carry_keeps_the_counts_and_drops_the_give_up_ceiling(tmp_path):
    """The pure half of the clear (``_pr_doctor_lifetime_carry``). Two invariants:

    * the engine's give-up keys (``total_attempts`` / ``total_mechanical``,
      ``attempts``, ``attempted``, ``gave_up``, ``last_*``) are NOT carried — the
      clear must still hand a recovered PR a fresh budget, exactly as before;
    * a target the doctor never acted on carries nothing, so its clear is still a
      byte-for-byte ``{}`` and the seam still answers UNKNOWN for it.
    """
    orch = _orch_with_ctx()
    carry = orch._pr_doctor_lifetime_carry

    assert carry({}) == {}
    assert carry({"last_head_sha": "abc", "attempts": 0}) == {}
    assert carry({"attempted": True, "attempts": 1, "total_attempts": 1,
                  "last_action_sha": "sha1", "gave_up": False}) == {
        "lifetime_attempts": 1, "lifetime_mechanical": 0}
    assert carry({"total_mechanical": 2}) == {
        "lifetime_attempts": 0, "lifetime_mechanical": 2}
    # Cumulative across repeated recoveries.
    assert carry({"lifetime_attempts": 2, "lifetime_mechanical": 1,
                  "total_attempts": 1, "total_mechanical": 3}) == {
        "lifetime_attempts": 3, "lifetime_mechanical": 4}
    # A garbage value from an old row degrades to 0 — the clear must never raise
    # inside the PR poller's transition callback.
    assert carry({"total_attempts": "x", "total_mechanical": None}) == {}


def test_both_tap_spawn_paths_record_a_running_outcome(tmp_path, monkeypatch):
    """The propose tap and manual dispatch are dispatches too (spec §6): each
    leaves the same ``running`` row the loop's auto path writes, carrying the
    product's model and the claim's attempt count. Only the claim WITHOUT a spawn
    (the loop's propose branch) records nothing."""
    from types import SimpleNamespace

    from sqlalchemy import select

    from server.db.models import DispatchOutcome

    async def run():
        await _init(tmp_path, "tap_outcomes.db")
        await _seed_product("widget", "o/r", policy={
            "dispatch": "auto", "default_model": "opus", "max_in_flight": 2})
        await _set_runner_row(config={"max_concurrent": 10, "max_spawns_per_day": 10})

        async def resolve_repos(names):
            return {name: name.casefold() for name in names}

        monkeypatch.setattr("server.automations.product_admission.resolve_repositories",
                            resolve_repos)
        await _seed_item("o/r", 1)
        await _seed_item("o/r", 2)
        orch = _orch_with_ctx()

        async def read_config():
            return "on", {**_config(), "max_concurrent": 10, "max_spawns_per_day": 10}
        orch._backlog_read_config = read_config

        async def query_queue(_config, **_kw):
            return [{"ref": "o/r#1", "repo": "o/r", "title": "one",
                     "product": "widget"},
                    {"ref": "o/r#2", "repo": "o/r", "title": "two",
                     "product": "widget"}]
        orch._backlog_query_queue = query_queue

        async def announce(_batch):
            return None
        orch._backlog_announce = announce

        async def account_for(_ctx, _ref):
            return "acct"
        orch._automation_account_for = account_for

        spawned: list = []

        async def spawn(batch, account, *, default_model, ownership_token):
            spawned.append((batch.primary, account, default_model))
            return SimpleNamespace(id=f"sess-{len(spawned)}", branch="b")
        orch._backlog_spawn = spawn
        from unittest.mock import AsyncMock
        from server.automations.backlog_runner import BacklogRunnerLoop
        orch._backlog_count_in_flight = AsyncMock(return_value=0)
        orch._backlog_loop = BacklogRunnerLoop(
            orch._backlog_ctx, select_batch=AsyncMock(return_value=None), spawn=spawn,
            count_in_flight=orch._backlog_count_in_flight, healthy_account=AsyncMock(),
            pr_status_for=AsyncMock(), pr_doctor_active=AsyncMock(),
            read_config=read_config, admission=orch._backlog_admission, clock=lambda: 1)

        # The claim the tap spawns for already carries one spent attempt.
        await orch._backlog_ctx.set_target_state("o/r#1", {
            "repo": "o/r", "product": "widget", "attempts": 1,
            "issues": ["o/r#1"], "claimed": True, "proposed": True})

        assert await orch.backlog_runner_implement_core("o/r#1") == "spawned"
        assert (await orch.backlog_runner_dispatch(["o/r#2"]))["status"] == "spawned"

        async with db_session() as db:
            rows = {r.target_ref: r for r in (await db.execute(
                select(DispatchOutcome))).scalars().all()}
        assert set(rows) == {"o/r#1", "o/r#2"}
        tapped, dispatched = rows["o/r#1"], rows["o/r#2"]
        assert tapped.outcome == "running" and tapped.session_id == "sess-1"
        assert tapped.model == "opus" and tapped.attempt_no == 1
        assert tapped.project_id is not None and tapped.body_fingerprint is not None
        assert dispatched.outcome == "running" and dispatched.session_id == "sess-2"
        assert dispatched.model == "opus" and dispatched.attempt_no == 0

    asyncio.run(run())
