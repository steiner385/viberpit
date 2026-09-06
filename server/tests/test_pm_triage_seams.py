"""Orchestrator pm-triage seams (design §6, phase-3 Task 3).

Bare-orchestrator + real-SQLite tests, in the mold of
``test_pm_orchestrator_seams.py``: the seams' DB reads/writes, with the spawn
paths stubbed to recorders.

The centre of gravity is ``open_map_diff_suggestions``. ``no_fit`` clusters are
permanent (accepting never clears ``no_fit_suggestion``), the drift branch is
NOT day-capped, and ``AutomationContext.spawn_session`` dedupes only against
NON-terminal sessions — so a seam that leaves the latch empty re-opens an
archaeology-diff PR for the same capability on every tick after the previous
session finishes, and the engine's own tests cannot notice (the field defaults
to ``()``). Delete the latch population in
``SessionOrchestrator._pm_triage_open_map_diffs`` and the three latch tests
below fail.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from server.automations.context import AutomationContext
from server.automations.pm_triage import PendingItemView
from server.db import db_session, init_db
from server.db.models import (
    Feature, Project, ProjectRepo, Session as DbSession, TriageProposal, WorkItem)

REPO = "steiner385/ccswitch"
OTHER_REPO = "steiner385/kindash"


def _bare_orch():
    from server.sessions.orchestrator import SessionOrchestrator
    return SessionOrchestrator.__new__(SessionOrchestrator)


class _Emitter:
    """Fake AutomationContext capturing emit() calls (no DB writes)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def emit(self, level, summary, **kw):
        self.events.append((level, summary, kw))


class _SpawnRec:
    """Fake AutomationContext capturing spawn_session calls."""

    def __init__(self):
        self.calls = []

    async def spawn_session(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(id=f"sess-{len(self.calls)}", model=None)


@pytest.fixture
def db_env():
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite+aiosqlite:///{tmp}/t.db"
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": url, "CCSWITCH_DEV": "1",
                                     "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            yield url


async def _seed(db_url: str) -> None:
    """Two projects, one repo each — the second exists so the ``Feature.slug``
    scoping (unique per project only) has something to get wrong."""
    await init_db(db_url)
    async with db_session() as db:
        p = Project(slug="ccswitch", name="ccswitch", onboarding_state="map_merged")
        other = Project(slug="kindash", name="kindash", onboarding_state="map_merged")
        db.add_all([p, other])
        await db.flush()
        db.add(ProjectRepo(project_id=p.id, repo=REPO, is_primary=True,
                           backfill_done=True))
        db.add(ProjectRepo(project_id=other.id, repo=OTHER_REPO, is_primary=True,
                           backfill_done=True))


async def _add_cluster(*, suggestion: str, numbers: list[int], repo: str = REPO,
                       status: str = "accepted") -> None:
    """One accepted-no_fit cluster: N work items each with an accepted proposal
    carrying *suggestion*."""
    async with db_session() as db:
        for n in numbers:
            w = WorkItem(repo=repo, number=n, kind="issue", title=f"i{n}",
                         state="open", triage_state="classified")
            db.add(w)
            await db.flush()
            db.add(TriageProposal(work_item_id=w.id, status=status,
                                  no_fit_suggestion=suggestion))


async def _clusters(orch):
    state = await orch._pm_triage_count_state()
    return state, {(c.suggestion, c.repo): c.item_numbers
                   for c in state.no_fit_clusters}


def _triage_orch(*, onboard=None, emitter=None):
    orch = _bare_orch()
    # The real context: _existing_session is the SAME lookup spawn_session
    # dedupes with, and the latch must not drift from it.
    orch._pm_onboard_ctx = onboard if onboard is not None else AutomationContext(
        "pm-onboard")
    orch._pm_triage_ctx = emitter if emitter is not None else _Emitter()
    return orch


# repo → project ownership itself is NOT tested here: the orchestrator no longer
# owns a resolver to test. Every seam below routes through the one implementation
# in ``server/pm/repo_project.py`` (``test_pm_repo_project.py`` pins the
# lowest-slug tie-break and the batch shape); the seams' own contract — resolve,
# and SKIP rather than guess when no project owns the repo — stays pinned by
# ``test_classify_skips_when_repo_maps_to_no_project``,
# ``test_propose_map_diff_skips_when_repo_maps_to_no_project`` and
# ``test_cluster_repo_with_no_project_is_latched``.


# --------------------------------------------------------------------------- #
# open_map_diff_suggestions — the latch                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_latches_suggestion_whose_feature_already_exists(db_env):
    """(a) The map diff merged: pm-sync indexed a Feature with that slug in the
    owning project, so the capability is on the map. Re-proposing it would open
    a second archaeology PR for a feature that already exists."""
    await _seed(db_env)
    await _add_cluster(suggestion="offline-mode", numbers=[1, 2, 3, 4, 5])
    async with db_session() as db:
        from sqlalchemy import select
        pid = (await db.execute(select(Project.id).where(
            Project.slug == "ccswitch"))).scalars().one()
        db.add(Feature(project_id=pid, slug="offline-mode", title="Offline",
                       state="live"))
    orch = _triage_orch()
    state, _ = await _clusters(orch)
    assert state.open_map_diff_suggestions == ("offline-mode",)


@pytest.mark.asyncio
async def test_feature_in_another_project_does_not_latch(db_env):
    """``Feature.slug`` is unique per (project_id, slug) only — an unscoped
    lookup latches on another product's feature and the capability is never
    proposed for THIS project."""
    await _seed(db_env)
    await _add_cluster(suggestion="offline-mode", numbers=[1, 2, 3, 4, 5])
    async with db_session() as db:
        from sqlalchemy import select
        other = (await db.execute(select(Project.id).where(
            Project.slug == "kindash"))).scalars().one()
        db.add(Feature(project_id=other, slug="offline-mode", title="Offline",
                       state="live"))
    orch = _triage_orch()
    state, _ = await _clusters(orch)
    assert state.open_map_diff_suggestions == ()


@pytest.mark.asyncio
async def test_latches_suggestion_with_a_live_map_diff_session(db_env):
    """(b) A non-terminal map-diff session for this suggestion is in flight."""
    await _seed(db_env)
    await _add_cluster(suggestion="offline-mode", numbers=[1, 2, 3, 4, 5])
    async with db_session() as db:
        db.add(DbSession(name="pm map-diff ccswitch", account_name="a1",
                         cwd="/tmp/x", status="working", origin="pm-onboard",
                         origin_ref="ccswitch:map-diff:offline-mode"))
    orch = _triage_orch()
    state, _ = await _clusters(orch)
    assert state.open_map_diff_suggestions == ("offline-mode",)


@pytest.mark.asyncio
async def test_terminal_map_diff_session_does_not_latch(db_env):
    """A FINISHED map-diff session is exactly the case the spawn-time dedupe
    misses; the (a) half (a merged Feature) is what stops the re-propose, not
    this one. An ended session on its own must not latch."""
    await _seed(db_env)
    await _add_cluster(suggestion="offline-mode", numbers=[1, 2, 3, 4, 5])
    async with db_session() as db:
        db.add(DbSession(name="pm map-diff ccswitch", account_name="a1",
                         cwd="/tmp/x", status="ended", origin="pm-onboard",
                         origin_ref="ccswitch:map-diff:offline-mode"))
    orch = _triage_orch()
    state, _ = await _clusters(orch)
    assert state.open_map_diff_suggestions == ()


@pytest.mark.asyncio
async def test_unlatched_cluster_is_proposed_by_the_engine(db_env):
    """The negative control: nothing latched → the engine picks the drift
    branch. If the latch over-fires, this test catches it."""
    from server.pm.engine import ProposeFeatureMapDiff, next_triage_action
    await _seed(db_env)
    await _add_cluster(suggestion="offline-mode", numbers=[1, 2, 3, 4, 5])
    orch = _triage_orch()
    state, _ = await _clusters(orch)
    assert state.open_map_diff_suggestions == ()
    # mode comes from the (absent) automation row, i.e. "off" — flip it here so
    # the engine gets past its first branch and actually exercises drift.
    action = next_triage_action(replace(state, mode="propose"),
                                {"drift_threshold": 5})
    assert isinstance(action, ProposeFeatureMapDiff)
    assert action.suggestion == "offline-mode" and action.repo == REPO


@pytest.mark.asyncio
async def test_latched_cluster_is_not_re_proposed(db_env):
    """End-to-end of the latch: the same over-threshold cluster stops driving
    the drift branch once its Feature exists."""
    from server.pm.engine import next_triage_action
    await _seed(db_env)
    await _add_cluster(suggestion="offline-mode", numbers=[1, 2, 3, 4, 5])
    async with db_session() as db:
        from sqlalchemy import select
        pid = (await db.execute(select(Project.id).where(
            Project.slug == "ccswitch"))).scalars().one()
        db.add(Feature(project_id=pid, slug="offline-mode", title="Offline",
                       state="live"))
    orch = _triage_orch()
    state, _ = await _clusters(orch)
    action = next_triage_action(replace(state, mode="propose"),
                                {"drift_threshold": 5})
    from server.pm.engine import Idle
    assert isinstance(action, Idle), action


@pytest.mark.asyncio
async def test_cluster_repo_with_no_project_is_latched(db_env):
    """A cluster whose repo maps to no project can never be spawned (the seam
    can only skip). Latch it, or it monopolises the drift branch forever and
    starves clusters that CAN be proposed."""
    await _seed(db_env)
    await _add_cluster(suggestion="orphan-cap", numbers=[1, 2, 3, 4, 5],
                       repo="someone/unregistered")
    orch = _triage_orch()
    state, _ = await _clusters(orch)
    assert state.open_map_diff_suggestions == ("orphan-cap",)


# --------------------------------------------------------------------------- #
# count_state — clustering                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_clusters_group_by_suggestion_and_repo(db_env):
    """Grouping by suggestion ALONE would name one repo while carrying the
    other's issue numbers — and a map-diff PR targets exactly one repo, so
    those numbers resolve to unrelated issues there."""
    await _seed(db_env)
    await _add_cluster(suggestion="offline-mode", numbers=[1, 2], repo=REPO)
    await _add_cluster(suggestion="offline-mode", numbers=[7, 8],
                       repo=OTHER_REPO)
    orch = _triage_orch()
    _state, by_key = await _clusters(orch)
    assert by_key == {("offline-mode", REPO): (1, 2),
                      ("offline-mode", OTHER_REPO): (7, 8)}


@pytest.mark.asyncio
async def test_pending_proposals_are_not_clusters(db_env):
    """Only ACCEPTED no_fit proposals are drift — a pending one is still
    awaiting the human gate."""
    await _seed(db_env)
    await _add_cluster(suggestion="offline-mode", numbers=[1, 2],
                       status="pending")
    orch = _triage_orch()
    state, by_key = await _clusters(orch)
    assert by_key == {}
    assert state.pending_count == 2


# --------------------------------------------------------------------------- #
# count_state — unclassified accounting                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unclassified_excludes_needs_human_and_proposed(db_env):
    await _seed(db_env)
    async with db_session() as db:
        db.add(WorkItem(repo=REPO, number=1, kind="issue", title="fresh",
                        state="open", triage_state="unclassified"))
        db.add(WorkItem(repo=REPO, number=2, kind="issue", title="rejected",
                        state="open", triage_state="needs_human"))
        db.add(WorkItem(repo=REPO, number=3, kind="issue", title="closed",
                        state="closed", triage_state="unclassified"))
        proposed = WorkItem(repo=REPO, number=4, kind="issue", title="proposed",
                            state="open", triage_state="unclassified")
        db.add(proposed)
        await db.flush()
        db.add(TriageProposal(work_item_id=proposed.id, status="pending"))
    orch = _triage_orch()
    state, _ = await _clusters(orch)
    assert state.unclassified_count == 1
    assert state.mode == "off"  # no automation row → never proposes
    # The loop owns the spawn ledger; the seam must not pre-spend the budget.
    assert state.day_spawns == 0


# --------------------------------------------------------------------------- #
# list_pending                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_pending_filters_the_candidate_set(db_env):
    await _seed(db_env)
    async with db_session() as db:
        db.add(WorkItem(repo=REPO, number=1, kind="pr", title="a pr",
                        state="open", triage_state="unclassified",
                        labels=["bug"], linked_issue_numbers=[42]))
        db.add(WorkItem(repo=REPO, number=2, kind="issue", title="rejected",
                        state="open", triage_state="needs_human"))
        proposed = WorkItem(repo=REPO, number=3, kind="issue", title="proposed",
                            state="open", triage_state="unclassified")
        db.add(proposed)
        await db.flush()
        db.add(TriageProposal(work_item_id=proposed.id, status="pending"))
    orch = _triage_orch()
    out = await orch._pm_triage_list_pending()
    assert [(i.number, i.kind) for i in out] == [(1, "pr")]
    assert out[0].labels == ("bug",) and out[0].linked_issue_numbers == (42,)


# --------------------------------------------------------------------------- #
# auto_link                                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_auto_link_inherits_the_closed_issues_feature(db_env):
    await _seed(db_env)
    async with db_session() as db:
        from sqlalchemy import select
        pid = (await db.execute(select(Project.id).where(
            Project.slug == "ccswitch"))).scalars().one()
        f = Feature(project_id=pid, slug="sessions", title="Sessions",
                    state="live")
        db.add(f)
        await db.flush()
        db.add(WorkItem(repo=REPO, number=42, kind="issue", title="the issue",
                        state="closed", triage_state="classified",
                        feature_id=f.id))
        db.add(WorkItem(repo=REPO, number=99, kind="pr", title="the pr",
                        state="open", triage_state="unclassified",
                        linked_issue_numbers=[42]))
        feature_id = f.id
    orch = _triage_orch()
    item = PendingItemView(repo=REPO, number=99, kind="pr", title="the pr",
                           body="", labels=(), linked_issue_numbers=(42,))
    assert await orch._pm_triage_auto_link(item) is True
    async with db_session() as db:
        from sqlalchemy import select
        pr = (await db.execute(select(WorkItem).where(
            WorkItem.repo == REPO, WorkItem.number == 99))).scalars().one()
        assert pr.feature_id == feature_id
        assert pr.triage_state == "classified"
        assert pr.classified_by == "auto-link"
        assert pr.classified_at is not None


@pytest.mark.asyncio
async def test_auto_link_skips_unclassified_issue(db_env):
    await _seed(db_env)
    async with db_session() as db:
        db.add(WorkItem(repo=REPO, number=42, kind="issue", title="the issue",
                        state="open", triage_state="unclassified"))
        db.add(WorkItem(repo=REPO, number=99, kind="pr", title="the pr",
                        state="open", triage_state="unclassified",
                        linked_issue_numbers=[42]))
    orch = _triage_orch()
    item = PendingItemView(repo=REPO, number=99, kind="pr", title="the pr",
                           body="", labels=(), linked_issue_numbers=(42,))
    assert await orch._pm_triage_auto_link(item) is False


# --------------------------------------------------------------------------- #
# classify / propose_map_diff — reuse of the phase-2 seams                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_classify_routes_to_pm_classify_run_scoped_to_one_project(db_env):
    await _seed(db_env)
    orch = _triage_orch()
    seen: dict = {}

    async def fake_run(slug, *, items=None):
        seen["slug"] = slug
        seen["items"] = items
        return len(items or [])

    orch.pm_classify_run = fake_run
    batch = [
        PendingItemView(repo=REPO, number=1, kind="issue", title="", body="",
                        labels=(), linked_issue_numbers=()),
        PendingItemView(repo=OTHER_REPO, number=2, kind="issue", title="",
                        body="", labels=(), linked_issue_numbers=()),
    ]
    assert await orch._pm_triage_classify(batch, 2) == 1
    assert seen["slug"] == "ccswitch"
    # The other project's item is NOT classified against ccswitch's feature map.
    assert seen["items"] == [(REPO, 1, "issue")]


@pytest.mark.asyncio
async def test_classify_skips_when_repo_maps_to_no_project(db_env):
    await _seed(db_env)
    emitter = _Emitter()
    orch = _triage_orch(emitter=emitter)

    async def boom(*a, **kw):  # pragma: no cover — must never be called
        raise AssertionError("classify must not spawn for an unowned repo")

    orch.pm_classify_run = boom
    batch = [PendingItemView(repo="someone/unknown", number=1, kind="issue",
                             title="", body="", labels=(),
                             linked_issue_numbers=())]
    assert await orch._pm_triage_classify(batch, 1) is None
    assert any(kw.get("kind") == "skipped" for _l, _s, kw in emitter.events)


@pytest.mark.asyncio
async def test_propose_map_diff_reuses_the_phase2_map_seam(db_env):
    await _seed(db_env)
    rec = _SpawnRec()
    orch = _triage_orch(onboard=rec)
    orch._backlog_healthy_account = _mk_async("acct1")
    orch._effective_repo_root = _mk_async("/tmp/root")
    orch._sdlc_default_branch = _mk_async("master")
    with patch("ccswitch.gitrepos.ensure_clone", return_value="/tmp/clone"):
        out = await orch._pm_triage_propose_map_diff("offline-mode", (1, 2), REPO)
    assert out is not None
    assert rec.calls[0]["origin_ref"] == "ccswitch:map-diff:offline-mode"
    assert rec.calls[0]["dedupe_key"] == "ccswitch:map-diff:offline-mode"
    assert rec.calls[0]["repo"] == REPO
    # scope="diff" must NOT advance onboarding_state (pinned in the phase-2
    # seam's own tests; asserted here because this caller is the reason it is
    # parameterized).
    async with db_session() as db:
        from sqlalchemy import select
        p = (await db.execute(select(Project).where(
            Project.slug == "ccswitch"))).scalars().one()
        assert p.onboarding_state == "map_merged"


@pytest.mark.asyncio
async def test_propose_map_diff_skips_when_repo_maps_to_no_project(db_env):
    await _seed(db_env)
    rec = _SpawnRec()
    emitter = _Emitter()
    orch = _triage_orch(onboard=rec, emitter=emitter)
    out = await orch._pm_triage_propose_map_diff("orphan-cap", (1, 2),
                                                 "someone/unknown")
    assert out is None
    assert rec.calls == []
    assert any(kw.get("kind") == "skipped" for _l, _s, kw in emitter.events)


# --------------------------------------------------------------------------- #
# mark_stale                                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_mark_stale_persists_nothing_and_emits(db_env):
    await _seed(db_env)
    emitter = _Emitter()
    orch = _triage_orch(emitter=emitter)
    await orch._pm_triage_mark_stale(())
    assert emitter.events == []
    await orch._pm_triage_mark_stale(("sessions", "billing"))
    assert len(emitter.events) == 1
    level, summary, kw = emitter.events[0]
    assert level == "info" and "sessions" in summary
    assert kw["kind"] == "stale_features"
    assert kw["detail"] == {"slugs": ["sessions", "billing"]}


# --------------------------------------------------------------------------- #
# wiring                                                                      #
# --------------------------------------------------------------------------- #


def test_pm_triage_task_is_primary_only():
    """A worker daemon must not run the propose loop: two daemons against one
    shared DB would open duplicate map-diff PRs."""
    from server.sessions.orchestrator import _PRIMARY_ONLY_LOOP_ATTRS
    assert "_pm_triage_task" in _PRIMARY_ONLY_LOOP_ATTRS


def _mk_async(value):
    async def _f(*a, **kw):
        return value
    return _f
