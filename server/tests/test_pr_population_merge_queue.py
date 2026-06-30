# server/tests/test_pr_population_merge_queue.py
from datetime import datetime, timezone
import pytest
from server.github.pr_population import PrPopulationPoller
from server.github.merge_queue import QueueEntry

_FIXED_NOW = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)  # epoch 1782820800

@pytest.mark.asyncio
async def test_refresh_snapshots_merge_queue():
    captured = {}
    def qfn(repo):  # population query — return empty population
        return {"data": {"repo": {"pullRequests": {"nodes": []}, "deployments": {"nodes": []}}}}
    def mqfn(repos):
        assert repos == ["acme/web"]
        return {"data": {"r0": {"mergeQueue": {"entries": {"nodes": [
            {"position": 1, "state": "AWAITING_CHECKS", "enqueuedAt": "2026-06-30T10:00:00Z",
             "estimatedTimeToMerge": 60,
             "headCommit": {"oid": "x", "statusCheckRollup": {"state": "PENDING",
                "contexts": {"totalCount": 2, "nodes": [
                    {"__typename": "CheckRun", "status": "COMPLETED"},
                    {"__typename": "CheckRun", "status": "IN_PROGRESS"}]}}},
             "pullRequest": {"number": 7}}]}}}}}
    async def sink(by_repo, captured_at):
        captured["by_repo"] = by_repo
        captured["at"] = captured_at
    p = PrPopulationPoller(query_fn=qfn, merge_queue_query_fn=mqfn, merge_queue_sink=sink,
                           now_fn=lambda: _FIXED_NOW)
    await p.refresh_once(["acme/web"])
    assert captured["by_repo"]["acme/web"][0] == QueueEntry(
        pr_number=7, position=1, state="AWAITING_CHECKS", enqueued_at=1782813600,
        batch_head_oid="x", group_checks="pending", checks_total=2, checks_done=1,
        eta_seconds=60)
    assert captured["at"] == 1782820800

@pytest.mark.asyncio
async def test_refresh_ok_when_merge_queue_fns_absent():
    def qfn(repo):
        return {"data": {"repo": {"pullRequests": {"nodes": []}, "deployments": {"nodes": []}}}}
    p = PrPopulationPoller(query_fn=qfn)         # no merge-queue wiring
    assert await p.refresh_once(["acme/web"]) is True
