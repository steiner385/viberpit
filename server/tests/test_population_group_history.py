"""Integration: a population cycle with a green merge group records a GroupRun.

Exercises PrPopulationPoller._enrich_with_eta -> _record_group_history ->
group_history.record_group_outcome, writing into the async ccswitch.db (db_session).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from server.db import db_session
from server.db.models import GroupFailure, GroupRun
from server.github.pr_population import PrPopulationPoller

REPO = "o/n"
NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fresh_db(isolated_db):
    """Every test gets its own DB — no cross-file order dependence (#728)."""


def _pop_resp(number: int) -> dict:
    return {"data": {"repo": {
        "deployments": {"nodes": []},
        "pullRequests": {"nodes": [{
            "number": number, "url": f"u{number}", "title": "t", "state": "OPEN",
            "isDraft": False, "updatedAt": "2026-07-12T00:00:00Z", "mergedAt": None,
            "headRefOid": "x", "mergeStateStatus": "CLEAN",
            "commits": {"nodes": [{"commit": {"statusCheckRollup":
                {"state": "PENDING", "contexts": {"nodes": []}}}}]},
        }]},
    }, "merged": {"nodes": []}}}


def _eta_resp_green_group(prs: list[tuple[str, int]], oid: str) -> dict:
    """Stub eta response: the PR is enqueued in a fully-green merge group."""
    data: dict[str, dict] = {}
    for i, (_repo, _number) in enumerate(prs):
        data[f"r{i}"] = {"pullRequest": {
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {
                "contexts": {"nodes": []}}}}]},
            "mergeQueueEntry": {
                "estimatedTimeToMerge": 300,
                "headCommit": {"oid": oid, "statusCheckRollup": {"contexts": {"nodes": [
                    {"__typename": "CheckRun", "name": "build", "status": "COMPLETED",
                     "conclusion": "SUCCESS", "startedAt": "2026-07-12T11:00:00Z",
                     "completedAt": "2026-07-12T11:10:00Z",
                     "checkSuite": {"workflowRun": {"workflow": {"name": "CI"}}}},
                ]}}},
            },
        }}
    return {"data": data}


def test_population_cycle_records_group_run():
    async def run():
        p = PrPopulationPoller(
            query_fn=lambda repo: _pop_resp(42),
            eta_query_fn=lambda prs: _eta_resp_green_group(prs, "batchoid1"),
            history_store=None,
            now_fn=lambda: NOW,
        )
        await p.refresh_once([REPO])
        async with db_session() as db:
            runs = (await db.execute(
                select(GroupRun).where(GroupRun.repo == REPO)
            )).scalars().all()
            assert len(runs) == 1
            assert runs[0].duration_secs == 600  # 11:00 -> 11:10
            assert runs[0].completed_at == "2026-07-12T11:10:00Z"
            assert (await db.execute(select(GroupFailure))).scalars().all() == []

    asyncio.run(run())


def test_population_cycle_never_breaks_on_history_error(monkeypatch):
    """A group-history write blowing up must not fail the poll cycle."""
    async def run():
        import server.pipeline.group_history as gh

        async def boom(*a, **k):
            raise RuntimeError("db down")

        monkeypatch.setattr(gh, "record_group_outcome", boom)
        p = PrPopulationPoller(
            query_fn=lambda repo: _pop_resp(42),
            eta_query_fn=lambda prs: _eta_resp_green_group(prs, "batchoid2"),
            history_store=None,
            now_fn=lambda: NOW,
        )
        ok = await p.refresh_once([REPO])
        assert ok is True
        # The PR is still cached despite the history failure.
        assert len(p.cached(REPO)) == 1

    asyncio.run(run())
