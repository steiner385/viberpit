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
            parent_slug=None, links=(),
        ),),
        orphan_slugs=(),
        revive_slugs=(),
        warnings=(),
    )
    await orch._pm_sync_apply_feature_plan(project_id, plan)

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
            parent_slug=None, links=(),
        ),),
        orphan_slugs=(),
        revive_slugs=(),
        warnings=(),
    )
    await orch._pm_sync_apply_feature_plan(project_id, plan)

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
    plan = FeatureIndexPlan(upserts=[], orphan_slugs=(), revive_slugs=(), warnings=())
    await orch._pm_sync_apply_feature_plan(project_id, plan)

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
    orch._chained_session_output_text = _mk_async('```json\n[{"number": 1}]\n```')
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


@pytest.mark.asyncio
async def test_backlog_verify_ready_seam_threads_batch_issues_and_gh_run():
    """The runner's live re-check (spec 003 §7a safety net) must run the real
    ``verify_ready_live`` over the WHOLE batch with the orchestrator's ``gh``
    seam, off the event loop, and pass its tri-state answer through unchanged
    (``None`` stays ``None`` — the loop is what fails closed on it)."""
    from server.automations.backlog_selection import Batch
    orch = _bare_orch()
    sentinel_gh = object()
    orch._gh_run = sentinel_gh
    seen: list = []

    def fake_verify(refs, *, gh_run):
        seen.append((list(refs), gh_run))
        return None

    batch = Batch(primary="o/r#1", issues=["o/r#1", "o/r#2"], repo="o/r")
    with patch("server.automations.backlog_github.verify_ready_live", fake_verify):
        assert await orch._backlog_verify_ready(batch) is None
    assert seen == [(["o/r#1", "o/r#2"], sentinel_gh)]


@pytest.mark.asyncio
async def test_backlog_announce_threads_batch_and_gh_run_into_announce_claim():
    """Phase D: the claim is the durable row, and the orchestrator seam only
    announces it — the whole batch and the orchestrator's ``gh`` seam go into
    ``announce_claim``, with a comment naming the runner and the primary."""
    from server.automations.backlog_selection import Batch
    orch = _bare_orch()
    sentinel_gh = object()
    orch._gh_run = sentinel_gh
    seen: list = []

    def fake_announce(batch, comment, *, gh_run):
        seen.append((list(batch.issues), comment, gh_run))

    batch = Batch(primary="o/r#1", issues=["o/r#1", "o/r#2"], repo="o/r")
    with patch("server.automations.backlog_github.announce_claim", fake_announce):
        assert await orch._backlog_announce(batch) is None
    (issues, comment, gh_run) = seen[0]
    assert issues == ["o/r#1", "o/r#2"] and gh_run is sentinel_gh
    assert "Backlog Runner" in comment and "o/r#1" in comment


@pytest.mark.asyncio
async def test_backlog_needs_human_threads_batch_and_reason(db_env):
    """The give-up announcement carries the REASON (what a human needs in order
    to pick the target up) and the batch reconstructed from the durable row."""
    from server.automations.context import AutomationContext
    await init_db(db_env)
    orch = _bare_orch()
    orch._gh_run = object()
    orch._backlog_ctx = AutomationContext("backlog-runner")
    async with db_session() as db:
        from server.db.models import Automation
        db.add(Automation(id="backlog-runner"))
    await orch._backlog_ctx.set_target_state(
        "o/r#1", {"repo": "o/r", "issues": ["o/r#1", "o/r#2"], "gave_up": True})
    seen: list = []

    def fake_announce(batch, reason, *, gh_run):
        seen.append((list(batch.issues), reason, gh_run))

    with patch("server.automations.backlog_github.announce_needs_human", fake_announce):
        await orch._backlog_needs_human("o/r#1", "max attempts (2) reached")
    assert seen == [(["o/r#1", "o/r#2"], "max attempts (2) reached", orch._gh_run)]


def test_backlog_queue_tail_returns_copy_of_cache():
    """The status-card queue-provenance seam (#990) must hand the loop a COPY
    of the cached ranked pool — mutating what it returns must never corrupt
    ``_backlog_last_queue``, which ``_backlog_select_batch`` /
    ``_backlog_refresh_groups`` / the card's counts all share."""
    orch = _bare_orch()
    cached = [{"ref": "o/r#1", "score": 5, "product": "widget"}]
    orch._backlog_last_queue = cached
    tail = orch._backlog_queue_tail()
    assert tail == cached
    tail.append({"ref": "o/r#2", "score": 1, "product": "widget"})
    assert orch._backlog_last_queue == cached  # unaffected by the caller's mutation


# --------------------------------------------------------------------------- #
# GraphQL transport: partial per-alias errors reach the reconciler (#1508)     #
# --------------------------------------------------------------------------- #

_PARTIAL = ('{"data": {"m0": {"issue": {"id": "I_1"}}, "m1": null}, '
            '"errors": [{"path": ["m1"], "message": "Validation failed: this dependency '
            'would create a cycle where the target is already blocked by the source"}]}')


def _gh(stdout: str, returncode: int, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


@pytest.mark.asyncio
async def test_reconcile_mutate_returns_payload_with_per_alias_errors():
    """``gh api graphql`` exits 1 whenever the response carries ``errors`` — even when
    every other alias succeeded. The mutate seam must hand that payload to the
    reconciler (which maps errors per alias and parks a mutation on its second
    failure) instead of raising and aborting the whole product cycle."""
    orch = _bare_orch()
    seen: list = []

    def gh_run(args, token=None):
        seen.append((list(args), token))
        return _gh(_PARTIAL, 1, "gh: Validation failed: this dependency would create a cycle")

    orch._gh_run = gh_run
    with patch.dict(os.environ, {"CCSWITCH_GH_PROJECT_TOKEN": "tok-pat"}):
        payload = await orch._pm_reconcile_mutate("steiner385", "mutation { m0: … }")
    assert payload["errors"][0]["path"] == ["m1"]
    assert payload["data"]["m0"]["issue"]["id"] == "I_1"
    assert seen[0][1] == "tok-pat"


@pytest.mark.asyncio
async def test_reconcile_mutate_still_raises_on_transport_failure():
    orch = _bare_orch()
    orch._gh_run = lambda args, token=None: _gh("", 1, "gh: HTTP 502 Bad Gateway")
    with patch.dict(os.environ, {"CCSWITCH_GH_PROJECT_TOKEN": "tok-pat"}):
        with pytest.raises(RuntimeError, match="502"):
            await orch._pm_reconcile_mutate("steiner385", "mutation { m0: … }")


@pytest.mark.asyncio
async def test_mirror_read_seam_stays_strict_on_graphql_errors():
    """Reads keep failing loudly: a payload with errors could read as 'board deleted'
    to the mirror (node → null), so the read seams must not swallow it."""
    orch = _bare_orch()
    orch._gh_run = lambda args, token=None: _gh(_PARTIAL, 1, "gh: Validation failed")
    with patch.dict(os.environ, {"CCSWITCH_GH_PROJECT_TOKEN": "tok-pat"}):
        with pytest.raises(RuntimeError):
            await orch._pm_sync_fetch_projects("steiner385", "query { … }")
    with pytest.raises(RuntimeError):
        await orch._pm_sync_fetch("query { … }")


# --------------------------------------------------------------------------- #
# Plan materialization (spec 003 §7b, #992)                                    #
# --------------------------------------------------------------------------- #

_DECOMP = """Here is how I would split it.

```json
{"epic_title": "Dark mode", "epic_body": "Ship dark mode.",
 "tasks": [{"title": "Tokens", "body": "b1", "depends_on": []},
           {"title": "Toggle", "body": "b2", "depends_on": [0]},
           {"title": "Docs", "body": "b3"}]}
```
"""


async def _seed_initiative(db_url: str, *, status: str = "awaiting_human") -> str:
    from server.db.models import Initiative
    await init_db(db_url)
    async with db_session() as db:
        ini = Initiative(slug="dark-mode", repo="o/r", title="Dark mode",
                         stage="plan", status=status)
        db.add(ini)
        await db.flush()
        return ini.id


def _materialize_orch(text: str = _DECOMP):
    orch = _bare_orch()
    orch._sdlc_synth_ctx = FakeAutomationContext()
    orch._chained_session_output_text = _mk_async(text)
    return orch


@pytest.mark.asyncio
async def test_await_session_settled_returns_true_once_settled(monkeypatch):
    orch = _bare_orch()
    statuses = ["running", "running", "complete"]
    orch.get_session = lambda sid: SimpleNamespace(
        id=sid, status=statuses.pop(0) if statuses else "complete")
    import asyncio as _a
    monkeypatch.setattr(_a, "sleep", _mk_async(None))
    assert await orch._await_session_settled("s1", attempts=10, interval_s=0.0) is True


@pytest.mark.asyncio
async def test_await_session_settled_gives_up_after_attempts(monkeypatch):
    orch = _bare_orch()
    orch.get_session = lambda sid: SimpleNamespace(id=sid, status="running")
    import asyncio as _a
    monkeypatch.setattr(_a, "sleep", _mk_async(None))
    assert await orch._await_session_settled("s1", attempts=3, interval_s=0.0) is False


@pytest.mark.asyncio
async def test_materialize_ingest_writes_one_pending_proposal(db_env):
    from server.db.models import MaterializationProposal
    ini_id = await _seed_initiative(db_env)
    orch = _materialize_orch()
    assert await orch._sdlc_materialize_ingest("sess-9", "dark-mode") == 1
    async with db_session() as db:
        from sqlalchemy import select
        rows = (await db.execute(select(MaterializationProposal))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.initiative_id == ini_id
    assert row.status == "pending"
    assert row.session_id == "sess-9"
    assert len(row.payload["tasks"]) == 3
    assert row.payload["epic_title"] == "Dark mode"
    assert row.payload["tasks"][1]["depends_on"] == [0]
    assert row.payload["source_session"] == "sess-9"
    assert row.rationale and row.rationale.startswith("Here is how")


@pytest.mark.asyncio
async def test_materialize_ingest_is_idempotent_while_pending(db_env):
    from server.db.models import MaterializationProposal
    await _seed_initiative(db_env)
    orch = _materialize_orch()
    assert await orch._sdlc_materialize_ingest("sess-9", "dark-mode") == 1
    assert await orch._sdlc_materialize_ingest("sess-10", "dark-mode") == 0
    async with db_session() as db:
        from sqlalchemy import select
        rows = (await db.execute(select(MaterializationProposal))).scalars().all()
    assert len(rows) == 1
    kinds = [e[2].get("kind") for e in orch._sdlc_synth_ctx.events]
    assert "materialization_duplicate_ignored" in kinds


@pytest.mark.asyncio
async def test_materialize_ingest_parse_failure_blocks_the_initiative(db_env):
    from server.db.models import Initiative, MaterializationProposal
    await _seed_initiative(db_env)
    orch = _materialize_orch("I could not do it, sorry.")
    assert await orch._sdlc_materialize_ingest("sess-9", "dark-mode") == 0
    async with db_session() as db:
        from sqlalchemy import select
        assert (await db.execute(select(MaterializationProposal))).scalars().all() == []
        ini = (await db.execute(select(Initiative).where(
            Initiative.slug == "dark-mode"))).scalar_one()
        assert ini.status == "blocked"
    ev = next(e for e in orch._sdlc_synth_ctx.events
              if e[2].get("kind") == "materialization_parse_failed")
    assert ev[0] == "warn"
    assert ev[2]["detail"]["error"]


@pytest.mark.asyncio
async def test_materialize_chain_ingest_swallows_ingest_failures(db_env, monkeypatch):
    """An exception inside the chained task must never escape — the task is
    fire-and-forget, so an escape is an unretrievable 'never retrieved' log."""
    orch = _bare_orch()
    orch.get_session = lambda sid: SimpleNamespace(id=sid, status="complete")
    orch._sdlc_synth_ctx = FakeAutomationContext()

    async def boom(*a, **k):
        raise RuntimeError("db is gone")

    orch._sdlc_materialize_ingest = boom
    import asyncio as _a
    monkeypatch.setattr(_a, "sleep", _mk_async(None))
    assert await orch._sdlc_materialize_chain_ingest("s1", "dark-mode",
                                                     attempts=2, delay=0.0) == 0


_MAT_INI = {"id": "i1", "slug": "dark-mode", "repo": "o/r", "title": "Dark mode"}


def _spawn_materialize_orch(ctx=None, *, plan_body="# the plan", config=None):
    orch = _bare_orch()
    orch._sdlc_synth_ctx = ctx or FakeAutomationContext()
    orch._automation_account_for = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    orch._sdlc_synth_read_config = _mk_async(("propose", config or {}))
    orch._sdlc_show_committed_file = lambda cwd, ref, path: f"{plan_body} at {ref}:{path}"
    orch.chained: list = []

    async def fake_chain(session_id, slug, **kw):
        orch.chained.append((session_id, slug))
        return 0

    orch._sdlc_materialize_chain_ingest = fake_chain
    orch.scheduled: list = []
    orch._spawn = lambda coro, name=None: orch.scheduled.append(coro)
    return orch


@pytest.mark.asyncio
async def test_spawn_materialize_seeds_the_plan_and_chains_the_ingest(db_env):
    await _seed_initiative(db_env)
    orch = _spawn_materialize_orch()
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        rec = await orch._sdlc_spawn_materialize(_MAT_INI)
    assert rec is not None
    call = orch._sdlc_synth_ctx.calls[0]
    assert call["origin_ref"] == "dark-mode:materialize"
    assert call["dedupe_key"] == "dark-mode:materialize"
    assert call["cwd"] == "/tmp/clone"
    assert "docs/sdlc/dark-mode/plan.md" in call["initial_prompt"]
    assert "pull request" in call["initial_prompt"]
    assert len(orch.scheduled) == 1
    await orch.scheduled[0]
    assert orch.chained == [("sess-1", "dark-mode")]


async def _seed_materialize_session(status: str) -> None:
    """A decompose session for dark-mode in *status*, as the spawn gate sees it."""
    from server.db.models import Session as DbSession
    async with db_session() as db:
        db.add(DbSession(id=f"sess-{status}", name="sdlc materialize dark-mode",
                         account_name="acct1", cwd="/tmp/clone", status=status,
                         origin="sdlc-synthesizer",
                         origin_ref="dark-mode:materialize"))
        await db.commit()


@pytest.mark.asyncio
async def test_spawn_materialize_skips_a_live_decompose_session(db_env):
    """The dedupe key would return the SAME session from spawn_session, and the
    caller would then chain a second ingest onto it and re-emit. Asking first
    makes a tick during the decompose session a no-op (C4-R13a)."""
    await _seed_initiative(db_env)
    await _seed_materialize_session("in_progress")
    ctx = FakeAutomationContext()
    orch = _spawn_materialize_orch(ctx)
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        assert await orch._sdlc_spawn_materialize(_MAT_INI) is None
    assert ctx.calls == []
    assert ctx.plans == []  # not even the launch plan was paid for
    assert orch.scheduled == []


@pytest.mark.asyncio
async def test_spawn_materialize_ignores_a_finished_decompose_session(db_env):
    """C4-R14: "already handled" is a session that still HOLDS the work — the
    live set, not ``_existing_session``'s not-ended rule. A rejected proposal's
    ``complete`` session must not block the next decompose forever."""
    await _seed_initiative(db_env)
    await _seed_materialize_session("complete")
    orch = _spawn_materialize_orch()
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        rec = await orch._sdlc_spawn_materialize(_MAT_INI)
    assert rec is not None
    for coro in orch.scheduled:
        coro.close()


@pytest.mark.asyncio
async def test_spawn_materialize_waits_on_a_session_that_needs_a_human(db_env):
    """A decompose session parked on a question still holds the plan."""
    await _seed_initiative(db_env)
    await _seed_materialize_session("waiting_question")
    ctx = FakeAutomationContext()
    orch = _spawn_materialize_orch(ctx)
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        assert await orch._sdlc_spawn_materialize(_MAT_INI) is None
    assert ctx.plans == []


@pytest.mark.asyncio
async def test_spawn_materialize_skips_an_unreadable_plan(db_env):
    """Seeding the fallback text would have the session invent a decomposition
    out of the slug — worse than no proposal."""
    await _seed_initiative(db_env)
    ctx = FakeAutomationContext()
    orch = _spawn_materialize_orch(ctx, plan_body="(could not inline")
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        assert await orch._sdlc_spawn_materialize(_MAT_INI) is None
    assert ctx.calls == []
    assert orch.scheduled == []
    ev = next(e for e in ctx.events if e[2].get("kind") == "materialization_plan_unreadable")
    assert ev[0] == "warn"


@pytest.mark.asyncio
async def test_spawn_materialize_honours_the_pinned_model(db_env):
    """Same model policy as the spec/plan sessions — an operator pinning a model
    for the synthesizer must reach the decompose session too."""
    await _seed_initiative(db_env)
    orch = _spawn_materialize_orch(config={"default_model": "opus"})
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        await orch._sdlc_spawn_materialize(_MAT_INI)
    for coro in orch.scheduled:  # the chain is asserted elsewhere; don't leak it
        coro.close()
    call = orch._sdlc_synth_ctx.calls[0]
    assert call["model"] == "opus"
    assert call["detail"]["model_source"] == "pinned"


@pytest.mark.asyncio
async def test_spawn_materialize_refused_launch_spends_nothing(db_env):
    await _seed_initiative(db_env)
    orch = _bare_orch()
    orch._sdlc_synth_ctx = FakeAutomationContext(refuse=True)
    assert await orch._sdlc_spawn_materialize(_MAT_INI) is None
    assert orch._sdlc_synth_ctx.calls == []


@pytest.mark.asyncio
async def test_has_pending_materialization_tracks_the_proposal(db_env):
    """The loop's spawn gate and the ingest's duplicate guard must answer off the
    same query — this is the loop half of it."""
    ini_id = await _seed_initiative(db_env)
    orch = _materialize_orch()
    assert await orch._sdlc_has_pending_materialization(ini_id) is False
    assert await orch._sdlc_materialize_ingest("sess-9", "dark-mode") == 1
    assert await orch._sdlc_has_pending_materialization(ini_id) is True


@pytest.mark.asyncio
async def test_spawn_materialize_warns_once_per_slug_about_an_unreadable_plan(db_env):
    """Every tick would otherwise re-emit the same warn forever — the same
    once-per-slug suppression the loop's `materialization_unwired` warn has."""
    await _seed_initiative(db_env)
    ctx = FakeAutomationContext()
    orch = _spawn_materialize_orch(ctx, plan_body="(could not inline")
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        assert await orch._sdlc_spawn_materialize(_MAT_INI) is None
        assert await orch._sdlc_spawn_materialize(_MAT_INI) is None
    warns = [e for e in ctx.events if e[2].get("kind") == "materialization_plan_unreadable"]
    assert len(warns) == 1


async def _seed_proposal(db_url: str) -> tuple[str, str]:
    from server.db.models import MaterializationProposal
    ini_id = await _seed_initiative(db_url)
    async with db_session() as db:
        prop = MaterializationProposal(
            initiative_id=ini_id, status="pending", session_id="sess-9",
            payload={"epic_title": "Dark mode", "epic_body": "Ship it.",
                     "tasks": [{"title": "Tokens", "body": "b0", "depends_on": []},
                               {"title": "Toggle", "body": "b1", "depends_on": [0]}],
                     "source_session": "sess-9"})
        db.add(prop)
        await db.flush()
        return ini_id, prop.id


def _apply_orch(payloads):
    """Orchestrator whose mutate seam replays *payloads* (one per document)."""
    orch = _materialize_orch()
    orch.mutated: list = []

    async def fake_mutate(owner, doc):
        orch.mutated.append((owner, doc))
        return payloads.pop(0)

    orch._pm_reconcile_mutate = fake_mutate
    return orch


def _issue(n: int) -> dict:
    return {"data": {"createIssue": {"issue": {
        "id": f"I_{n}", "number": 100 + n,
        "url": f"https://github.com/o/r/issues/{100 + n}"}}}}


@pytest.mark.asyncio
async def test_materialize_apply_seam_emits_materialized(db_env):
    """The API calls THIS; the router must never build a GitHub call itself."""
    from server.db.models import Initiative, MaterializationProposal
    ini_id, prop_id = await _seed_proposal(db_env)
    orch = _apply_orch([
        {"data": {"repository": {"id": "R_1"}}}, _issue(1), _issue(2), _issue(3),
        {"data": {"m0": {"clientMutationId": None}}},
    ])
    out = await orch._sdlc_materialize_apply(prop_id)
    assert out["ok"] is True
    assert out["tasks"] == 2 and out["edges"] == 1
    ev = next(e for e in orch._sdlc_synth_ctx.events if e[2].get("kind") == "materialized")
    assert ev[0] == "info"
    assert ev[2]["detail"]["tasks"] == 2
    async with db_session() as db:
        from sqlalchemy import select
        ini = (await db.execute(select(Initiative).where(
            Initiative.id == ini_id))).scalar_one()
        assert (ini.epic_repo, ini.epic_number) == ("o/r", 101)
        prop = (await db.execute(select(MaterializationProposal).where(
            MaterializationProposal.id == prop_id))).scalar_one()
        assert prop.status == "accepted"


@pytest.mark.asyncio
async def test_materialize_apply_seam_emits_the_failure(db_env):
    _, prop_id = await _seed_proposal(db_env)
    orch = _apply_orch([
        {"data": {"repository": {"id": "R_1"}}}, _issue(1),
        {"data": {"createIssue": None}, "errors": [{"message": "nope"}]},
    ])
    out = await orch._sdlc_materialize_apply(prop_id)
    assert out["ok"] is False
    assert out["step"] == "task:0"
    ev = next(e for e in orch._sdlc_synth_ctx.events
              if e[2].get("kind") == "materialization_failed")
    assert ev[0] == "warn"
    assert ev[2]["detail"]["step"] == "task:0"


@pytest.mark.asyncio
async def test_materialize_apply_seam_404s_on_an_unknown_proposal(db_env):
    await _seed_initiative(db_env)
    orch = _apply_orch([])
    out = await orch._sdlc_materialize_apply("nope")
    assert out == {"ok": False, "step": "load", "error": "no such proposal: nope"}
