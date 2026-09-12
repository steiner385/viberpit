"""The runner's orchestrator seams read PRODUCT policies (spec §5, phase 1).

``Project.automation_policy`` — not the runner's retired global repo knobs — now says
which products the Backlog Runner works, how far down each board it reaches, each
product's in-flight cap/weight and its attempt cap/model. These tests drive the four
seams that translate policies into the shapes the pure selection code already takes,
plus the one-shot seed that carries the old ``enabled_repos`` settings across the flip.

A real (SQLite) mirror is seeded and read — no GitHub call is involved.
"""
from __future__ import annotations

import asyncio

from server.db import db_session, init_db
from server.db.models import (AuditEvent, AutomationEvent, GhProject, GhProjectItem,
                              Project, ProjectRepo, WorkItem)


def _orch_with_ctx():
    import server.sessions.orchestrator as orch_mod
    from server.automations.context import AutomationContext
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._backlog_ctx = AutomationContext("backlog-runner")
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


def test_count_candidates_returns_per_product_rows_with_in_flight_and_cap(tmp_path):
    async def run():
        await _init(tmp_path, "counts.db")
        await _seed_product("alpha", "o/a",
                            policy={"dispatch": "auto", "max_in_flight": 2})
        await _seed_item("o/a", 1, score=5.0)
        await _seed_item("o/a", 2, score=4.0)
        orch = _orch_with_ctx()

        async def _read_config():
            return "on", _config()
        orch._backlog_read_config = _read_config
        # One live claim, tagged with the product that owns it.
        await orch._backlog_ctx.set_target_state(
            "o/a#9", {"claimed": True, "issues": ["o/a#9"], "product": "alpha"})

        eligible, blocked, untriaged, per_product = \
            await orch._backlog_count_candidates()

        assert (eligible, blocked, untriaged) == (2, 0, 0)
        assert per_product["alpha"]["in_flight"] == 1
        assert per_product["alpha"]["cap"] == 2

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
        monkeypatch.setattr("ccswitch.gitrepos.stale_credential_repos", lambda: [])

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
        assert alpha == {"dispatch": "auto", "queue_min_status": "Backlog",
                         "max_in_flight": 4, "weight": 3, "max_attempts": 5,
                         "default_model": "opus", "respect_dependencies": False}
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


def test_seed_is_a_no_op_once_any_product_has_a_policy(tmp_path):
    """One-shot by construction: a human who has already set a policy owns the
    answer, and a second seed would overwrite it with the retired defaults."""
    async def run():
        await _init(tmp_path, "seedonce.db")
        await _seed_product("alpha", "o/a", policy={"dispatch": "off"})
        await _seed_product("beta", "o/b")
        orch = _orch_with_ctx()

        assert await orch._backlog_seed_policies({"enabled_repos": ["o/a", "o/b"]},
                                                 "auto") == 0
        policies = await _policies()
        assert policies["alpha"][0] == {"dispatch": "off"}    # untouched
        assert policies["beta"] == (None, None)

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
