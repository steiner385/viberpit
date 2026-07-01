# server/tests/test_pr_population_merge_queue.py
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import server.github.pr_population as pop
from server.github.merge_queue import QueueEntry, parse_merge_queue_response
from server.github.pr_population import PrPopulationPoller, _default_merge_queue_query

_FIXED_NOW = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)  # epoch 1782820800


def test_default_merge_queue_query_groups_by_owner_and_remaps_aliases(monkeypatch):
    """Repos spanning two owners each use their own owner's token, and the merged
    response is re-aliased to the original repo index so parse_* is unchanged."""
    seen_tokens = []

    def fake_token_for_owner(owner):
        return f"tok-{owner}"

    def fake_run_gh(args, token=None):
        seen_tokens.append(token)
        # Each per-owner subset here has one repo → alias r0; encode owner in the PR number.
        num = 1 if token == "tok-acme" else 2
        body = {"data": {"r0": {"mergeQueue": {"entries": {"nodes": [
            {"position": 1, "state": "AWAITING_CHECKS", "enqueuedAt": None,
             "estimatedTimeToMerge": None,
             "headCommit": {"oid": "x", "statusCheckRollup": None},
             "pullRequest": {"number": num}}]}}}}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(body), stderr="")

    monkeypatch.setattr(pop.app_auth, "token_for_owner", fake_token_for_owner)
    monkeypatch.setattr(pop.gitrepos, "_run_gh", fake_run_gh)

    repos = ["acme/web", "cairnea/KinDash"]
    resp = _default_merge_queue_query(repos)

    assert set(resp["data"].keys()) == {"r0", "r1"}          # remapped to global indices
    assert set(seen_tokens) == {"tok-acme", "tok-cairnea"}   # per-owner tokens used
    out = parse_merge_queue_response(resp, repos)
    assert out["acme/web"][0].pr_number == 1
    assert out["cairnea/KinDash"][0].pr_number == 2


def test_default_merge_queue_query_empty_repos_returns_empty():
    assert _default_merge_queue_query([]) == {}

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
