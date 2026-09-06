"""Orchestrator PM onboarding/classification seams (design 2026-08-31 §5).

Bare-orchestrator + real-SQLite tests: the seams' DB reads/writes and the
prompt/parse wiring, with spawn stubbed to a recorder."""
from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from server.db import db_session, init_db
from server.tests.fakes.fake_automation_ctx import FakeAutomationContext
from server.db.models import (
    Feature, Project, ProjectRepo, TriageProposal, WorkItem)


def _bare_orch():
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    return orch


async def _seed(db_url: str) -> str:
    await init_db(db_url)
    async with db_session() as db:
        p = Project(slug="ccswitch", name="ccswitch", onboarding_state="map_merged")
        db.add(p)
        await db.flush()
        db.add(ProjectRepo(project_id=p.id, repo="steiner385/ccswitch",
                           is_primary=True, backfill_done=True))
        db.add(Feature(project_id=p.id, slug="sessions", title="Sessions",
                       state="live", summary="Session manager."))
        for n in range(3):
            db.add(WorkItem(repo="steiner385/ccswitch", number=100 + n,
                            kind="issue", title=f"Bug {n}", state="open",
                            triage_state="unclassified"))
        return p.id


@pytest.fixture
def db_env():
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite+aiosqlite:///{tmp}/t.db"
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": url, "CCSWITCH_DEV": "1",
                                     "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            yield url


@pytest.mark.asyncio
async def test_spawn_map_sets_state_and_dedup(db_env):
    await _seed(db_env)
    orch = _bare_orch()
    rec = FakeAutomationContext()
    orch._pm_onboard_ctx = rec
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        out = await orch.pm_onboard_spawn_map("ccswitch")
    assert out is not None
    assert rec.calls[0]["origin_ref"] == "ccswitch:feature-map"
    assert rec.calls[0]["dedupe_key"] == "ccswitch:feature-map"
    async with db_session() as db:
        from sqlalchemy import select
        p = (await db.execute(select(Project).where(
            Project.slug == "ccswitch"))).scalar_one()
        assert p.onboarding_state == "map_proposed"


@pytest.mark.asyncio
async def test_spawn_map_no_account_returns_none(db_env):
    await _seed(db_env)
    orch = _bare_orch()
    orch._pm_onboard_ctx = FakeAutomationContext()
    orch._backlog_healthy_account = _mk_async(None)
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        assert await orch.pm_onboard_spawn_map("ccswitch") is None


@pytest.mark.asyncio
async def test_classify_run_dispatches_open_items(db_env):
    await _seed(db_env)
    orch = _bare_orch()
    rec = FakeAutomationContext()
    orch._pm_classify_ctx = rec
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        n = await orch.pm_classify_run("ccswitch")
    assert n == 3
    assert rec.calls[0]["origin_ref"].startswith("ccswitch:classify")
    async with db_session() as db:
        from sqlalchemy import select
        p = (await db.execute(select(Project).where(
            Project.slug == "ccswitch"))).scalar_one()
        assert p.onboarding_state == "classifying"


@pytest.mark.asyncio
async def test_spawn_map_diff_scope_dedup_and_no_state_change(db_env):
    # Phase-3 reuse: scope="diff" uses the map-diff dedupe key and must NOT
    # advance onboarding_state (the seam is one method, parameterized).
    await _seed(db_env)
    orch = _bare_orch()
    rec = FakeAutomationContext()
    orch._pm_onboard_ctx = rec
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        out = await orch.pm_onboard_spawn_map(
            "ccswitch", scope="diff", suggestion="billing",
            item_numbers=(100, 101), repo="steiner385/ccswitch")
    assert out is not None
    assert rec.calls[0]["dedupe_key"] == "ccswitch:map-diff:billing"
    async with db_session() as db:
        from sqlalchemy import select
        p = (await db.execute(select(Project).where(
            Project.slug == "ccswitch"))).scalar_one()
        assert p.onboarding_state == "map_merged"  # unchanged by diff scope


@pytest.mark.asyncio
async def test_classify_run_accepts_injected_batch(db_env):
    # Phase-3 reuse: items=[(repo, number, kind), ...] classifies exactly that set.
    await _seed(db_env)
    orch = _bare_orch()
    rec = FakeAutomationContext()
    orch._pm_classify_ctx = rec
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        n = await orch.pm_classify_run(
            "ccswitch",
            items=[("steiner385/ccswitch", 100, "issue"),
                   ("steiner385/ccswitch", 101, "issue")])
    assert n == 2  # exactly the injected set, not the full open chunk


@pytest.mark.asyncio
async def test_classify_ingest_writes_pending_proposals(db_env):
    await _seed(db_env)
    orch = _bare_orch()
    output = """```json
    [{"number": 100, "kind": "issue", "feature_slug": "sessions",
      "reach": 3, "impact": 2, "effort": 1, "confidence": 2,
      "rationale": "session work", "confidence_level": "high"},
     {"number": 101, "kind": "issue", "feature_slug": null,
      "no_fit_suggestion": "billing", "rationale": "no fit",
      "confidence_level": "low"}]
    ```"""
    n = await orch._pm_classify_ingest("sess-1", "ccswitch", output)
    assert n == 2
    async with db_session() as db:
        from sqlalchemy import select
        props = (await db.execute(select(TriageProposal))).scalars().all()
        assert {p.status for p in props} == {"pending"}
        by_num = {  # proposal → work item number, via join through work_items
            (await db.execute(select(WorkItem.number).where(
                WorkItem.id == p.work_item_id))).scalar_one(): p
            for p in props}
        assert by_num[100].proposed_feature_id is not None  # resolved to Feature
        assert by_num[101].proposed_feature_id is None       # no_fit
        assert by_num[101].no_fit_suggestion == "billing"

        # Fix 1: _pm_classify_ingest sets triage_state="proposed" as a scheduling
        # marker on every written WorkItem, but NEVER touches the SCORE overlay.
        wi_100 = (await db.execute(select(WorkItem).where(
            WorkItem.repo == "steiner385/ccswitch",
            WorkItem.number == 100))).scalar_one()
        wi_101 = (await db.execute(select(WorkItem).where(
            WorkItem.repo == "steiner385/ccswitch",
            WorkItem.number == 101))).scalar_one()
        # Both touched items advance to "proposed"
        assert wi_100.triage_state == "proposed"
        assert wi_101.triage_state == "proposed"
        # Un-touched item (102) stays "unclassified"
        wi_102 = (await db.execute(select(WorkItem).where(
            WorkItem.repo == "steiner385/ccswitch",
            WorkItem.number == 102))).scalar_one()
        assert wi_102.triage_state == "unclassified"
        # Score-overlay columns are all still None (human-gate intact)
        _score_overlay = ("feature_id", "reach", "impact", "effort",
                          "confidence", "classified_by", "classified_at")
        for wi in (wi_100, wi_101):
            for col in _score_overlay:
                assert getattr(wi, col) is None, (
                    f"WorkItem #{wi.number}.{col} should be None before human accept"
                )


@pytest.mark.asyncio
async def test_classify_ingest_is_idempotent(db_env):
    await _seed(db_env)
    orch = _bare_orch()
    output = ('[{"number": 100, "kind": "issue", "feature_slug": "sessions",'
              ' "rationale": "x", "confidence_level": "medium"}]')
    first = await orch._pm_classify_ingest("sess-1", "ccswitch", output)
    second = await orch._pm_classify_ingest("sess-1", "ccswitch", output)
    assert (first, second) == (1, 0)  # second pass writes nothing (pending exists)


@pytest.mark.asyncio
async def test_apply_feature_plan_map_proposed_advances_to_map_merged(db_env):
    """Bug #2 regression: applying ≥1 feature to a map_proposed project must
    advance onboarding_state to map_merged."""
    await init_db(db_env)
    async with db_session() as db:
        p = Project(slug="test-proj", name="test-proj",
                    onboarding_state="map_proposed")
        db.add(p)
        await db.flush()
        project_id = p.id

    orch = _bare_orch()

    # Build a minimal plan with one upsert (simulating an indexed feature doc).
    from server.pm.feature_index import FeatureIndexPlan, FeatureUpsert
    plan = FeatureIndexPlan(
        upserts=(FeatureUpsert(
            slug="core", title="Core", state="live",
            summary="Core feature.", repo_path="docs/product/features/core.md",
        ),),
        orphan_slugs=(),
        revive_slugs=(),
    )
    await orch._pm_sync_apply_feature_plan(project_id, plan, {})

    async with db_session() as db:
        from sqlalchemy import select
        proj = (await db.execute(
            select(Project).where(Project.id == project_id)
        )).scalar_one()
        assert proj.onboarding_state == "map_merged"


@pytest.mark.asyncio
async def test_apply_feature_plan_non_map_proposed_state_unchanged(db_env):
    """Bug #2 regression: applying a plan to a project NOT in map_proposed must
    leave onboarding_state unchanged (e.g. map_merged stays map_merged)."""
    await init_db(db_env)
    async with db_session() as db:
        p = Project(slug="stable-proj", name="stable-proj",
                    onboarding_state="map_merged")
        db.add(p)
        await db.flush()
        project_id = p.id

    orch = _bare_orch()

    from server.pm.feature_index import FeatureIndexPlan, FeatureUpsert
    plan = FeatureIndexPlan(
        upserts=(FeatureUpsert(
            slug="feature-a", title="A", state="live",
            summary="A feature.", repo_path="docs/product/features/a.md",
        ),),
        orphan_slugs=(),
        revive_slugs=(),
    )
    await orch._pm_sync_apply_feature_plan(project_id, plan, {})

    async with db_session() as db:
        from sqlalchemy import select
        proj = (await db.execute(
            select(Project).where(Project.id == project_id)
        )).scalar_one()
        assert proj.onboarding_state == "map_merged"  # unchanged


@pytest.mark.asyncio
async def test_apply_feature_plan_map_proposed_no_upserts_stays(db_env):
    """map_proposed project with 0 new features (empty plan) must NOT advance."""
    await init_db(db_env)
    async with db_session() as db:
        p = Project(slug="empty-proj", name="empty-proj",
                    onboarding_state="map_proposed")
        db.add(p)
        await db.flush()
        project_id = p.id

    orch = _bare_orch()

    from server.pm.feature_index import FeatureIndexPlan
    plan = FeatureIndexPlan(upserts=[], orphan_slugs=(), revive_slugs=())
    await orch._pm_sync_apply_feature_plan(project_id, plan, {})

    async with db_session() as db:
        from sqlalchemy import select
        proj = (await db.execute(
            select(Project).where(Project.id == project_id)
        )).scalar_one()
        assert proj.onboarding_state == "map_proposed"  # no features → no advance


def _mk_async(value):
    async def _f(*a, **k):
        return value
    return _f


@pytest.mark.asyncio
async def test_classify_chain_ingest_waits_for_settled_then_ingests_once(db_env, monkeypatch):
    """The chain polls the session until it settles, then harvests + ingests ONCE.

    This is the wiring the dogfood found missing: classify sessions completed but
    ``_pm_classify_ingest`` had no caller, so proposals never landed (0 of 13)."""
    orch = _bare_orch()
    statuses = ["running", "running", "complete"]
    def fake_get_session(sid):
        return SimpleNamespace(id=sid, status=statuses.pop(0) if statuses else "complete")
    orch.get_session = fake_get_session
    orch._pm_classify_output_text = _mk_async('```json\n[{"number": 1}]\n```')
    ingested = []
    async def fake_ingest(session_id, project_slug, output_text):
        ingested.append((session_id, project_slug, output_text)); return 1
    orch._pm_classify_ingest = fake_ingest
    import asyncio as _a
    monkeypatch.setattr(_a, "sleep", _mk_async(None))
    written = await orch._pm_classify_chain_ingest("sess-1", "ccswitch",
                                                  attempts=10, delay=0.0)
    assert written == 1
    assert ingested == [("sess-1", "ccswitch", '```json\n[{"number": 1}]\n```')]


@pytest.mark.asyncio
async def test_classify_chain_ingest_stops_when_session_vanishes(db_env, monkeypatch):
    orch = _bare_orch()
    orch.get_session = lambda sid: None
    ingested = []
    async def fake_ingest(*a):
        ingested.append(a); return 1
    orch._pm_classify_ingest = fake_ingest
    import asyncio as _a
    monkeypatch.setattr(_a, "sleep", _mk_async(None))
    written = await orch._pm_classify_chain_ingest("gone", "ccswitch",
                                                  attempts=3, delay=0.0)
    assert written == 0
    assert ingested == []


@pytest.mark.asyncio
async def test_classify_run_schedules_ingest_chain(db_env):
    """pm_classify_run must schedule the ingest chain for the spawned session —
    without it the session's JSON output is never harvested into proposals."""
    await _seed(db_env)
    orch = _bare_orch()
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._pm_classify_ctx = FakeAutomationContext()
    chained = []
    async def fake_chain(session_id, slug, **kw):
        chained.append((session_id, slug)); return 0
    orch._pm_classify_chain_ingest = fake_chain
    scheduled = []
    orch._spawn = lambda coro, name=None: scheduled.append(coro)
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        n = await orch.pm_classify_run("ccswitch")
    assert n > 0
    assert len(scheduled) == 1
    await scheduled[0]
    assert chained == [("sess-1", "ccswitch")]


def _git(*args, cwd):
    import subprocess
    subprocess.run(["git", "-C", cwd, *args], check=True,
                   capture_output=True, text=True)


def _make_feature_repo(root: str, slugs) -> str:
    """A real git repo with docs/product/features/<slug>.md committed, plus a
    ``refs/remotes/origin/master`` ref (what _pm_sync_read_feature_docs resolves)."""
    import os
    import subprocess
    clone = os.path.join(root, "clone")
    os.makedirs(os.path.join(clone, "docs", "product", "features"))
    subprocess.run(["git", "init", "-q", clone], check=True,
                   capture_output=True, text=True)
    _git("config", "user.email", "t@t", cwd=clone)
    _git("config", "user.name", "t", cwd=clone)
    for slug in slugs:
        p = os.path.join(clone, "docs", "product", "features", f"{slug}.md")
        with open(p, "w") as fh:
            fh.write(f"---\nslug: {slug}\ntitle: T{slug}\nstate: live\n---\n\nbody {slug}\n")
    _git("add", "-A", cwd=clone)
    _git("commit", "-q", "-m", "docs", cwd=clone)
    head = subprocess.run(["git", "-C", clone, "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    _git("update-ref", "refs/remotes/origin/master", head, cwd=clone)
    return clone


@pytest.mark.asyncio
async def test_read_feature_docs_returns_all_docs_from_pinned_snapshot(db_env):
    """The reader returns EVERY feature doc in the tree (atomic snapshot) — never
    a silent subset.  Guards the regression that orphaned 21 live features."""
    orch = _bare_orch()
    orch._effective_repo_root = _mk_async("/tmp/root")
    with tempfile.TemporaryDirectory() as root:
        clone = _make_feature_repo(root, ["alpha", "beta", "gamma"])
        with patch("ccswitch.gitrepos.ensure_clone", return_value=clone):
            docs = await orch._pm_sync_read_feature_docs("o/r")
    paths = sorted(p for p, _ in docs)
    assert paths == [
        "docs/product/features/alpha.md",
        "docs/product/features/beta.md",
        "docs/product/features/gamma.md",
    ]
    assert all("body" in text for _, text in docs)


@pytest.mark.asyncio
async def test_read_feature_docs_raises_on_partial_read(db_env, monkeypatch):
    """A single unreadable blob makes the whole read raise — never a silent
    subset.  Returning a subset here is what orphaned the missing valid docs."""
    import subprocess
    orch = _bare_orch()
    orch._effective_repo_root = _mk_async("/tmp/root")

    def cp(cmd, rc=0, out=""):
        return subprocess.CompletedProcess(
            cmd, rc, stdout=out, stderr=("boom" if rc else ""))

    def fake_run(cmd, *a, **k):
        if "fetch" in cmd:
            return cp(cmd)
        if "symbolic-ref" in cmd:
            return cp(cmd, 0, "")            # no origin/HEAD → fallback to origin/master
        if "rev-parse" in cmd:
            return cp(cmd, 0, "deadbeefcafe\n")
        if "ls-tree" in cmd:
            return cp(cmd, 0, "docs/product/features/a.md\n"
                              "docs/product/features/b.md\n")
        if "show" in cmd:
            if cmd[-1].endswith("a.md"):
                return cp(cmd, 0, "---\nslug: a\ntitle: A\nstate: live\n---\nx")
            return cp(cmd, 1)                # b.md blob read FAILS
        return cp(cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        with pytest.raises(RuntimeError):
            await orch._pm_sync_read_feature_docs("o/r")


@pytest.mark.asyncio
async def test_spawn_critic_dedupes_and_builds_prompt(db_env):
    orch = _bare_orch()
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    rec = FakeAutomationContext()
    orch._pm_onboard_ctx = rec
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        out = await orch._pm_onboard_spawn_critic(
            "ccswitch", "steiner385/cc-account-switcher", "pm/feature-map-ccswitch")
    assert out is not None
    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["origin_ref"] == "ccswitch:feature-map-critic"
    assert call["dedupe_key"] == "ccswitch:feature-map-critic"
    assert call["detail"]["kind"] == "feature-map-critic"
    assert "pm/feature-map-ccswitch" in call["initial_prompt"]


@pytest.mark.asyncio
async def test_spawn_critic_returns_none_without_account(db_env):
    orch = _bare_orch()
    orch._backlog_healthy_account = _mk_async(None)
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    orch._pm_onboard_ctx = FakeAutomationContext()
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        out = await orch._pm_onboard_spawn_critic("ccswitch", "o/r", "pm/feature-map-ccswitch")
    assert out is None


@pytest.mark.asyncio
async def test_chain_critic_waits_for_pr_then_spawns_once(db_env, monkeypatch):
    orch = _bare_orch()
    seen = {"pr": [None, None, object()]}   # PR appears on the 3rd poll
    def fake_pr_for(repo, branch):
        return seen["pr"].pop(0) if seen["pr"] else object()
    orch.pr_for = fake_pr_for
    spawned = []
    async def fake_spawn(slug, repo, branch):
        spawned.append((slug, repo, branch)); return object()
    orch._pm_onboard_spawn_critic = fake_spawn
    import asyncio as _a
    monkeypatch.setattr(_a, "sleep", _mk_async(None))   # no real waiting
    out = await orch._pm_onboard_chain_critic(
        "ccswitch", "o/r", "pm/feature-map-ccswitch", attempts=5, delay=0.0)
    assert out is not None
    assert spawned == [("ccswitch", "o/r", "pm/feature-map-ccswitch")]  # exactly once


@pytest.mark.asyncio
async def test_chain_critic_times_out_without_spawning(db_env, monkeypatch):
    orch = _bare_orch()
    orch.pr_for = lambda repo, branch: None   # PR never appears
    spawned = []
    async def fake_spawn(*a): spawned.append(a); return object()
    orch._pm_onboard_spawn_critic = fake_spawn
    import asyncio as _a
    monkeypatch.setattr(_a, "sleep", _mk_async(None))
    out = await orch._pm_onboard_chain_critic("s", "o/r", "b", attempts=3, delay=0.0)
    assert out is None
    assert spawned == []


async def _seed_project_for_map(db_url: str) -> str:
    """Project with slug 'ccswitch', primary repo 'steiner385/cc-account-switcher',
    state 'syncing' — ready for pm_onboard_spawn_map(scope='initial')."""
    await init_db(db_url)
    async with db_session() as db:
        p = Project(slug="ccswitch", name="ccswitch", onboarding_state="syncing")
        db.add(p)
        await db.flush()
        db.add(ProjectRepo(project_id=p.id,
                           repo="steiner385/cc-account-switcher",
                           is_primary=True, backfill_done=True))
        return p.id


@pytest.mark.asyncio
async def test_initial_map_spawn_schedules_critic_chain(db_env):
    proj_id = await _seed_project_for_map(db_env)  # noqa: F841
    orch = _bare_orch()
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    orch._pm_onboard_ctx = FakeAutomationContext()
    chained = []
    async def fake_chain(slug, repo, branch, **kw):
        chained.append((slug, repo, branch))
    orch._pm_onboard_chain_critic = fake_chain
    # capture the bg-task instead of really backgrounding it
    scheduled = []
    orch._spawn = lambda coro, name=None: scheduled.append(coro)
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        rec = await orch.pm_onboard_spawn_map("ccswitch", scope="initial")
    assert rec is not None
    assert len(scheduled) == 1          # exactly one chain task scheduled
    await scheduled[0]                  # run it
    assert chained == [("ccswitch", "steiner385/cc-account-switcher",
                        "pm/feature-map-ccswitch")]


@pytest.mark.asyncio
async def test_diff_map_spawn_does_not_schedule_critic(db_env):
    await _seed_project_for_map(db_env)
    orch = _bare_orch()
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    orch._pm_onboard_ctx = FakeAutomationContext()
    scheduled = []
    orch._spawn = lambda coro, name=None: scheduled.append(coro)
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        await orch.pm_onboard_spawn_map("ccswitch", scope="diff", suggestion="x")
    assert scheduled == []   # drift path never chains a critic
