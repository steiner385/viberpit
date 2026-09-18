"""Per-capability, per-tier dispatch evidence — the reads behind the D9 model-pick
floor (spec 2026-09-12 product automation policy §7, phase 4 task 1).

``tier_floor_from_stats`` is covered purely in ``test_pm_outcomes.py``; what needs a
real (SQLite) database is the JOIN it is fed: ``DispatchOutcome`` rows matched to one
capability's issues by ``owner/repo#N`` ref, counted per model tier. The exclusions
are the point — another capability's issues, dispatches that have not finished
(``running``) or never got to answer (``superseded``), and rows whose model maps to no
tier at all — so each is seeded and each must be absent from the counts.

No GitHub call is involved: the refs come from the seeded mirror rows.
"""
from __future__ import annotations

import pytest

from server.db import db_session, init_db
from server.db.models import DispatchOutcome, Feature, Project, WorkItem
from server.pm.outcomes import (
    TierStats, outcome_stats, tier_floor, tier_floor_for_dispatch)

REPO = "o/r"


@pytest.fixture
def db_env(tmp_path):
    """A private SQLite file per test; ``_seed`` initialises it inside the test's own
    event loop (an engine built in another loop cannot be used from this one)."""
    return f"sqlite+aiosqlite:///{tmp_path / 'outcome_stats.db'}"


async def _seed(db_url: str) -> tuple[str, str]:
    """One product, two capabilities, four mirrored issues and the dispatch history
    below. Returns ``(project_id, feature_id)`` for the capability under test.

    | ref  | capability | model  | outcome     | first pass | counted           |
    |------|------------|--------|-------------|------------|-------------------|
    | #1   | cap        | sonnet | no_pr       | –          | sonnet outcome    |
    | #2   | cap        | sonnet | needs_human | –          | sonnet outcome    |
    | #3   | cap        | opus   | merged      | True       | opus outcome+pass |
    | #3   | cap        | opus   | merged      | True       | opus outcome+pass |
    | #3   | cap        | opus   | running     | –          | no — not finished |
    | #3   | cap        | opus   | superseded  | –          | no — never ran    |
    | #1   | cap        | None   | merged      | True       | no — no tier      |
    | #9   | other      | haiku  | merged      | True       | no — other cap    |
    """
    await init_db(db_url=db_url)
    async with db_session() as db:
        project = Project(slug="viberpit", name="viberpit")
        db.add(project)
        await db.flush()
        cap = Feature(project_id=project.id, slug="cap", title="Capability")
        other = Feature(project_id=project.id, slug="other", title="Other")
        db.add_all([cap, other])
        await db.flush()
        for number in (1, 2, 3):
            db.add(WorkItem(repo=REPO, number=number, kind="issue",
                            title=f"issue {number}", body="b", feature_id=cap.id))
        db.add(WorkItem(repo=REPO, number=9, kind="issue", title="issue 9", body="b",
                        feature_id=other.id))

        def _outcome(number: int, model: str | None, outcome: str,
                     first_pass: bool | None = None) -> DispatchOutcome:
            return DispatchOutcome(
                automation_id="backlog-runner", project_id=project.id,
                target_ref=f"{REPO}#{number}", model=model, outcome=outcome,
                first_pass_merge=first_pass)

        db.add_all([
            _outcome(1, "sonnet", "no_pr"),
            _outcome(2, "sonnet", "needs_human"),
            _outcome(3, "opus", "merged", True),
            _outcome(3, "opus", "merged", True),
            _outcome(3, "opus", "running"),
            _outcome(3, "opus", "superseded"),
            _outcome(1, None, "merged", True),
            _outcome(9, "haiku", "merged", True),
        ])
        await db.flush()
        return project.id, cap.id


@pytest.mark.asyncio
async def test_outcome_stats_groups_finalised_rows_by_tier_for_one_capability(db_env):
    project_id, feature_id = await _seed(db_env)
    async with db_session() as db:
        stats = await outcome_stats(db, project_id, feature_id)
    # #9 belongs to another capability; the running, superseded and untiered rows
    # are not evidence. Emitted in TIER_ORDER (cheapest first), not insertion order.
    assert stats == [TierStats("sonnet", 2, 0), TierStats("opus", 2, 2)]


async def _seed_rows(db_url: str,
                     rows: list[tuple[str | None, str, bool | None]]) -> tuple[str, str]:
    """One product, one capability, one mirrored issue, and *rows* as
    ``(model, outcome, first_pass_merge)`` dispatches against it."""
    await init_db(db_url=db_url)
    async with db_session() as db:
        project = Project(slug="viberpit", name="viberpit")
        db.add(project)
        await db.flush()
        cap = Feature(project_id=project.id, slug="cap", title="Capability")
        db.add(cap)
        await db.flush()
        db.add(WorkItem(repo=REPO, number=1, kind="issue", title="issue 1", body="b",
                        feature_id=cap.id))
        db.add_all([
            DispatchOutcome(automation_id="backlog-runner", project_id=project.id,
                            target_ref=f"{REPO}#1", model=model, outcome=outcome,
                            first_pass_merge=first_pass)
            for model, outcome, first_pass in rows])
        await db.flush()
        return project.id, cap.id


@pytest.mark.asyncio
async def test_outcome_stats_drops_a_merged_row_whose_first_pass_is_unknown(db_env):
    """A ``merged`` row with ``first_pass_merge IS NULL`` (the PR-Doctor attempt
    read answered UNKNOWN) is evidence of NEITHER success nor failure, so it is
    excluded from both counts — counting it as an outcome with no first pass read
    a tier that actually merged as one that cannot do the work, and floored the
    pick above what the capability needs."""
    project_id, feature_id = await _seed_rows(db_env, [
        ("sonnet", "merged", None), ("sonnet", "merged", None),
        ("opus", "merged", True), ("opus", "merged", True)])
    async with db_session() as db:
        # sonnet contributes no entry at all — not TierStats("sonnet", 2, 0).
        assert await outcome_stats(db, project_id, feature_id) == [
            TierStats("opus", 2, 2)]
        # …so no cheaper tier is shown to fail and there is no floor to raise to.
        assert await tier_floor(db, project_id, feature_id) is None


@pytest.mark.asyncio
async def test_outcome_stats_counts_a_finalised_non_merge_with_no_first_pass_flag(db_env):
    """The other side of the rule: a finalised row that did NOT merge is a genuine
    failure whatever ``first_pass_merge`` holds, and keeps counting as an outcome
    with zero first passes."""
    project_id, feature_id = await _seed_rows(db_env, [("sonnet", "no_pr", None)])
    async with db_session() as db:
        assert await outcome_stats(db, project_id, feature_id) == [
            TierStats("sonnet", 1, 0)]


@pytest.mark.asyncio
async def test_outcome_stats_never_counts_an_investigate_dispatch(db_env):
    """Final review M2: the D9 floor was the one outcome reader without the
    implement-only predicate. An ``investigate`` dispatch (series item 4b) never
    opens a PR by design, so counting it would post a first-pass-less outcome at
    its tier and drag the floor down — here it would make sonnet read as a tier
    that cannot do the work and float the floor up to opus."""
    await init_db(db_env)
    async with db_session() as db:
        project = Project(slug="viberpit", name="viberpit")
        db.add(project)
        await db.flush()
        cap = Feature(project_id=project.id, slug="cap", title="Capability")
        db.add(cap)
        await db.flush()
        db.add(WorkItem(repo=REPO, number=1, kind="issue", title="issue 1", body="b",
                        feature_id=cap.id))
        db.add_all([
            # NULL predates the column and still reads as ``implement``.
            DispatchOutcome(automation_id="backlog-runner", project_id=project.id,
                            target_ref=f"{REPO}#1", model="sonnet", outcome="merged",
                            first_pass_merge=True),
            DispatchOutcome(automation_id="backlog-runner", project_id=project.id,
                            target_ref=f"{REPO}#1", model="sonnet", outcome="merged",
                            first_pass_merge=True, dispatch_kind="implement"),
            DispatchOutcome(automation_id="backlog-runner", project_id=project.id,
                            target_ref=f"{REPO}#1", model="sonnet", outcome="no_pr",
                            dispatch_kind="investigate"),
            DispatchOutcome(automation_id="backlog-runner", project_id=project.id,
                            target_ref=f"{REPO}#1", model="opus", outcome="merged",
                            first_pass_merge=True, dispatch_kind="implement"),
            DispatchOutcome(automation_id="backlog-runner", project_id=project.id,
                            target_ref=f"{REPO}#1", model="opus", outcome="merged",
                            first_pass_merge=True, dispatch_kind="implement"),
        ])
        await db.flush()
        assert await outcome_stats(db, project.id, cap.id) == [
            TierStats("sonnet", 2, 2), TierStats("opus", 2, 2)]
        # sonnet does the work, so the floor stays where it is.
        assert await tier_floor(db, project.id, cap.id) is None


@pytest.mark.asyncio
async def test_outcome_stats_is_empty_for_a_capability_with_no_issues(db_env):
    project_id, _ = await _seed(db_env)
    async with db_session() as db:
        assert await outcome_stats(db, project_id, "no-such-feature") == []


@pytest.mark.asyncio
async def test_tier_floor_reads_the_stats(db_env):
    project_id, feature_id = await _seed(db_env)
    async with db_session() as db:
        assert await tier_floor(db, project_id, feature_id) == "opus"


@pytest.mark.asyncio
async def test_tier_floor_for_dispatch_resolves_slug_and_ref(db_env):
    await _seed(db_env)
    async with db_session() as db:
        assert await tier_floor_for_dispatch(db, "viberpit", f"{REPO}#1") == "opus"
        # Nothing to resolve → no floor, never a guess.
        assert await tier_floor_for_dispatch(db, "viberpit", f"{REPO}#404") is None
        assert await tier_floor_for_dispatch(db, "nope", f"{REPO}#1") is None
        assert await tier_floor_for_dispatch(db, None, f"{REPO}#1") is None
        assert await tier_floor_for_dispatch(db, "viberpit", "garbage") is None


@pytest.mark.asyncio
async def test_tier_floor_for_dispatch_is_best_effort(db_env, monkeypatch):
    """§13: a failed history read yields no floor and never takes the spawn with it."""
    from server.pm import outcomes as oc

    async def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(oc, "outcome_stats", boom)
    await _seed(db_env)
    async with db_session() as db:
        assert await oc.tier_floor_for_dispatch(db, "viberpit", f"{REPO}#1") is None
