"""Tests for PullRequestInfo.check_groups field: serializer round-trip and population wiring."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone

from server.github.pr_status import CheckGroup, CheckRow, PullRequestInfo


# ---------------------------------------------------------------------------
# Serializer tests (Steps 1–4)
# ---------------------------------------------------------------------------

def test_pullrequestinfo_check_groups_roundtrip():
    g = CheckGroup(kind="head", checks=(
        CheckRow("lint", "CI", "COMPLETED", "success", 30, True, False),
    ), total_seconds=30, cost_usd=0.008, eta_seconds=None)
    pr = PullRequestInfo(number=1, url="u", state="open", checks="passing",
                         title="t", check_groups=(g,))
    d = pr.to_dict()
    assert d["check_groups"] == [g.to_dict()]
    assert PullRequestInfo.from_dict(d).check_groups == (g,)


def test_pullrequestinfo_check_groups_default_empty():
    pr = PullRequestInfo(number=1, url="u", state="open", checks="none", title="t")
    assert pr.to_dict()["check_groups"] == []


# ---------------------------------------------------------------------------
# Population integration test (Step 6)
# ---------------------------------------------------------------------------

REPO = "o/n"
NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)


def _build_db(p):
    """Seed a minimal pipeline_history.db and return a HistoryStore pointing at it."""
    from server.pipeline.history import HistoryStore
    c = sqlite3.connect(str(p))
    c.executescript("""
        CREATE TABLE check_durations (repo TEXT, check_name TEXT, event TEXT,
          duration_secs REAL, completed_at TEXT, conclusion TEXT, head_sha TEXT,
          run_attempt INTEGER, started_at TEXT, run_number INTEGER);
        CREATE TABLE runner_waits (repo TEXT, check_name TEXT, event TEXT,
          wait_secs REAL, started_at TEXT, pool TEXT);
    """)
    for i in range(8):
        c.execute("INSERT INTO check_durations VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (REPO, "ci", "pull_request", 100,
                   f"2026-06-2{i % 10}T00:00:00Z", "SUCCESS", "sha", 1, "x", i))
    c.commit()
    c.close()
    return HistoryStore(str(p))


def _pop_resp(number: int, state: str = "OPEN") -> dict:
    """Minimal population-query response for one open PR."""
    return {"data": {"repo": {
        "deployments": {"nodes": []},
        "pullRequests": {"nodes": [{
            "number": number, "url": f"u{number}", "title": "t", "state": state,
            "isDraft": False, "updatedAt": "2026-06-25T00:00:00Z", "mergedAt": None,
            "headRefOid": "x", "mergeStateStatus": "CLEAN",
            "commits": {"nodes": [{"commit": {"statusCheckRollup":
                {"state": "PENDING", "contexts": {"nodes": []}}}}]},
        }]},
    }, "merged": {"nodes": []}}}


def _eta_checks_resp_with_head_and_merge(prs: list[tuple[str, int]]) -> dict:
    """Stub: returns a response with one head CheckRun and a mergeQueueEntry block."""
    data: dict[str, dict] = {}
    for i, (_repo, number) in enumerate(prs):
        data[f"r{i}"] = {
            "pullRequest": {
                "commits": {"nodes": [{"commit": {"statusCheckRollup": {
                    "contexts": {"nodes": [{
                        "__typename": "CheckRun",
                        "name": "ci",
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                        "startedAt": "2026-06-25T11:58:00Z",
                        "completedAt": "2026-06-25T12:00:00Z",
                        "isRequired": True,
                        "checkSuite": {"workflowRun": {"workflow": {"name": "CI"}}},
                    }]}
                }}}]},
                "mergeQueueEntry": {
                    "estimatedTimeToMerge": 120,
                    "headCommit": {"statusCheckRollup": {
                        "contexts": {"nodes": [{
                            "__typename": "CheckRun",
                            "name": "merge-ci",
                            "status": "IN_PROGRESS",
                            "conclusion": None,
                            "startedAt": "2026-06-25T11:59:00Z",
                            "completedAt": None,
                            "checkSuite": {"workflowRun": {"workflow": {"name": "MergeCI"}}},
                        }]},
                    }},
                },
            }
        }
    return {"data": data}


def test_enrich_attaches_check_groups(tmp_path):
    """Population poller, given head checks + a mergeQueueEntry, attaches check_groups."""
    from server.github.pr_population import PrPopulationPoller

    store = _build_db(tmp_path / "ph.db")

    p = PrPopulationPoller(
        query_fn=lambda repo: _pop_resp(42),
        eta_query_fn=_eta_checks_resp_with_head_and_merge,
        history_store=store,
        now_fn=lambda: NOW,
    )
    asyncio.run(p.refresh_once([REPO]))

    prs = p.cached(REPO)
    assert len(prs) == 1
    pr = prs[0]
    assert pr.number == 42

    # check_groups must be populated (at least the head group)
    assert len(pr.check_groups) >= 1, f"expected check_groups; got {pr.check_groups!r}"
    assert pr.check_groups[0].kind == "head", (
        f"first group kind should be 'head', got {pr.check_groups[0].kind!r}"
    )
