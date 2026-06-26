"""Integration test: PrPopulationPoller enriches open PRs with ETA from HistoryStore.

Mirrors test_pr_population.py's injection style. Uses a real (temp) pipeline_history.db
seeded the same way test_pipeline_eta.py's _store fixture does, and a stubbed
eta-checks fetch so no network is needed.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone

from server.github.pr_population import PrPopulationPoller
from server.pipeline.history import HistoryStore


REPO = "o/n"
NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)


def _build_db(p) -> HistoryStore:
    """Seed a minimal pipeline_history.db and return a HistoryStore pointing at it."""
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


def _eta_checks_resp_empty(prs: list[tuple[str, int]]) -> dict:
    """Stub: returns a response where every PR has no check contexts (all queued)."""
    # build_eta_checks_query aliases each PR as r0, r1, …
    data = {}
    for i, (_repo, number) in enumerate(prs):
        data[f"r{i}"] = {
            "pullRequest": {
                "commits": {"nodes": [{"commit": {"statusCheckRollup": {
                    "contexts": {"nodes": []}
                }}}]}
            }
        }
    return {"data": data}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_open_pr_gets_eta_seconds(tmp_path):
    """Population poller, given a history store + stubbed checks, attaches eta_seconds."""
    store = _build_db(tmp_path / "ph.db")
    eta_calls: list = []

    def eta_qfn(prs: list[tuple[str, int]]) -> dict:
        eta_calls.append(list(prs))
        return _eta_checks_resp_empty(prs)

    p = PrPopulationPoller(
        query_fn=lambda repo: _pop_resp(42),
        eta_query_fn=eta_qfn,
        history_store=store,
        now_fn=lambda: NOW,
    )
    asyncio.run(p.refresh_once([REPO]))

    prs = p.cached(REPO)
    assert len(prs) == 1
    pr = prs[0]
    assert pr.number == 42
    # An open PR with history data should have a non-None eta_seconds.
    assert pr.eta_seconds is not None, f"expected eta_seconds; got {pr.eta_seconds}"
    # The eta-checks query was called once with the open PR's (repo, number).
    assert eta_calls == [[(REPO, 42)]]


def test_absent_history_db_skips_eta_no_crash(tmp_path):
    """When the history db file is absent, ETA enrichment is skipped — no crash."""
    absent_path = tmp_path / "missing.db"  # never created

    p = PrPopulationPoller(
        query_fn=lambda repo: _pop_resp(7),
        # history_store: pass the path so the poller tries (and fails gracefully)
        history_db_path=str(absent_path),
    )
    # Must not raise.
    asyncio.run(p.refresh_once([REPO]))

    prs = p.cached(REPO)
    assert len(prs) == 1
    assert prs[0].eta_seconds is None


def test_merged_pr_does_not_get_eta(tmp_path):
    """Merged PRs are not enriched (ETA is only meaningful for open PRs)."""
    store = _build_db(tmp_path / "ph.db")
    eta_calls: list = []

    def eta_qfn(prs: list[tuple[str, int]]) -> dict:
        eta_calls.append(list(prs))
        return _eta_checks_resp_empty(prs)

    p = PrPopulationPoller(
        query_fn=lambda repo: _pop_resp(99, state="MERGED"),
        eta_query_fn=eta_qfn,
        history_store=store,
        now_fn=lambda: NOW,
    )
    asyncio.run(p.refresh_once([REPO]))

    prs = p.cached(REPO)
    assert len(prs) == 1
    assert prs[0].eta_seconds is None
    # eta-checks query should not have been called (no open PRs).
    assert eta_calls == []


def test_eta_fetch_error_leaves_defaults(tmp_path):
    """If the ETA checks fetch raises, defaults are preserved and no crash."""
    store = _build_db(tmp_path / "ph.db")

    def bad_eta_qfn(prs):
        raise RuntimeError("gh exploded")

    p = PrPopulationPoller(
        query_fn=lambda repo: _pop_resp(10),
        eta_query_fn=bad_eta_qfn,
        history_store=store,
        now_fn=lambda: NOW,
    )
    asyncio.run(p.refresh_once([REPO]))

    prs = p.cached(REPO)
    assert len(prs) == 1
    assert prs[0].eta_seconds is None
    assert prs[0].eta_overdue is False


def test_default_dest_path_loaded_when_no_kwargs(tmp_path, monkeypatch):
    """Regression: PrPopulationPoller() with no history kwargs must load from default_dest().

    Before the fix, _store_init_attempted was True when both history_store and
    history_db_path were None, causing _store() to return None immediately and
    silently skip ETA enrichment in production.  After the fix the flag is only
    True when history_store is pre-supplied, so the default_dest() branch runs.
    """
    db_path = tmp_path / "pipeline_history.db"
    _build_db(db_path)  # seed real data so compute_pr_eta returns a value

    # Patch default_dest inside the history_migrate module (where _store() imports it from).
    import server.pipeline.history_migrate as hm
    monkeypatch.setattr(hm, "default_dest", lambda: str(db_path))

    eta_calls: list = []

    def eta_qfn(prs: list[tuple[str, int]]) -> dict:
        eta_calls.append(list(prs))
        return _eta_checks_resp_empty(prs)

    # No history_store, no history_db_path — the production construction.
    p = PrPopulationPoller(
        query_fn=lambda repo: _pop_resp(55),
        eta_query_fn=eta_qfn,
        now_fn=lambda: NOW,
    )
    asyncio.run(p.refresh_once([REPO]))

    prs = p.cached(REPO)
    assert len(prs) == 1
    pr = prs[0]
    # The eta-checks query must have been called (proves _store() didn't short-circuit).
    assert eta_calls == [[(REPO, 55)]], f"eta_qfn not called; got {eta_calls}"
    # And with history data present, the PR should get a non-None eta_seconds.
    assert pr.eta_seconds is not None, (
        f"expected eta_seconds from default_dest() path; got {pr.eta_seconds}"
    )
