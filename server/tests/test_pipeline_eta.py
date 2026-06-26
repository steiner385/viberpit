from datetime import datetime, timezone
import sqlite3
from server.pipeline.history import HistoryStore
from server.pipeline.eta import compute_pr_eta

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)


def _store(tmp_path):
    p = tmp_path / "ph.db"
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
                  ("o/n", "ci", "pull_request", 100, f"2026-06-2{i%10}T00:00:00Z",
                   "SUCCESS", "s", 1, "x", i))
    c.commit(); c.close()
    return HistoryStore(str(p))


def test_compute_pr_eta_queued_check(tmp_path):
    s = _store(tmp_path)
    # no live checks → expected 'ci' is queued → eta ≈ p50 (100)
    eta, percent, overdue = compute_pr_eta(s, "o/n", [], NOW)
    assert eta == 100 and percent == 0 and overdue is False
