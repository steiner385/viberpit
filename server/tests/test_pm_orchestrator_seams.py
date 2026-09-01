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
from server.db.models import (
    Feature, Project, ProjectRepo, TriageProposal, WorkItem)


class _SpawnRec:
    """Fake AutomationContext capturing spawn_session calls."""
    def __init__(self):
        self.calls = []

    async def spawn_session(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(id=f"sess-{len(self.calls)}", model=None)


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
    rec = _SpawnRec()
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
    orch._pm_onboard_ctx = _SpawnRec()
    orch._backlog_healthy_account = _mk_async(None)
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        assert await orch.pm_onboard_spawn_map("ccswitch") is None


@pytest.mark.asyncio
async def test_classify_run_dispatches_open_items(db_env):
    await _seed(db_env)
    orch = _bare_orch()
    rec = _SpawnRec()
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
    rec = _SpawnRec()
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
    rec = _SpawnRec()
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


def _mk_async(value):
    async def _f(*a, **k):
        return value
    return _f
