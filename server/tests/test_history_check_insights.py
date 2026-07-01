# server/tests/test_history_check_insights.py
import sqlite3
from server.pipeline.history import HistoryStore

_SCHEMA = (
    "CREATE TABLE check_durations (repo TEXT, check_name TEXT, event TEXT, "
    "duration_secs REAL, completed_at TEXT, conclusion TEXT, head_sha TEXT, "
    "run_attempt INTEGER, started_at TEXT, run_number INTEGER)"
)

def _store(tmp_path, rows):
    p = tmp_path / "ph.db"
    c = sqlite3.connect(str(p))
    c.execute(_SCHEMA)
    c.executemany(
        "INSERT INTO check_durations (repo,check_name,event,duration_secs,completed_at,"
        "conclusion,head_sha,run_attempt,run_number) VALUES (?,?,?,?,?,?,?,?,?)", rows)
    c.commit(); c.close()
    return HistoryStore(str(p))

def test_slowest_checks_ranks_by_p50_with_ratio(tmp_path):
    rows = [
        # slow check: durations 100,100,100,900 (p90 high → ratio > 1)
        ("o/n", "slow", "pr", 100, "2026-07-01T00:00:00Z", "success", "s1", 1, 1),
        ("o/n", "slow", "pr", 100, "2026-07-01T00:00:00Z", "success", "s2", 1, 2),
        ("o/n", "slow", "pr", 100, "2026-07-01T00:00:00Z", "success", "s3", 1, 3),
        ("o/n", "slow", "pr", 900, "2026-07-01T00:00:00Z", "success", "s4", 1, 4),
        # fast check
        ("o/n", "fast", "pr", 10, "2026-07-01T00:00:00Z", "success", "f1", 1, 5),
        ("o/n", "fast", "pr", 10, "2026-07-01T00:00:00Z", "success", "f2", 1, 6),
        # a FAILURE row must be ignored by the duration panel
        ("o/n", "fast", "pr", 9999, "2026-07-01T00:00:00Z", "failure", "f3", 1, 7),
    ]
    s = _store(tmp_path, rows)
    out = s.slowest_checks("o/n", since_iso="2026-01-01T00:00:00Z")
    assert [x.check_name for x in out] == ["slow", "fast"]      # ranked by p50 desc
    slow = out[0]
    assert slow.p50 == 100 and slow.p90 == 900 and slow.n == 4
    assert slow.ratio == 9.0
    assert out[1].n == 2                                        # failure row excluded

def test_slowest_checks_filters_by_window(tmp_path):
    rows = [
        ("o/n", "c", "pr", 50, "2026-06-01T00:00:00Z", "success", "a", 1, 1),  # old
        ("o/n", "c", "pr", 60, "2026-07-01T00:00:00Z", "success", "b", 1, 2),  # in window
    ]
    s = _store(tmp_path, rows)
    out = s.slowest_checks("o/n", since_iso="2026-06-15T00:00:00Z")
    assert len(out) == 1 and out[0].n == 1 and out[0].p50 == 60
