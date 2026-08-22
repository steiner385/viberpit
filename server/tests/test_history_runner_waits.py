import sqlite3
import threading

from server.pipeline.history import HistoryStore


def _store(rows):
    """rows: (repo, check_name, event, wait_secs, started_at, pool)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE runner_waits (repo TEXT, check_name TEXT, event TEXT, "
        "wait_secs REAL, started_at TEXT, pool TEXT)"
    )
    conn.executemany("INSERT INTO runner_waits VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    st = HistoryStore.__new__(HistoryStore)
    st._conn = conn
    st._lock = threading.Lock()
    return st


def test_runner_wait_health_p50_p90_per_event():
    # current window (>= since): pull_request waits 10,20,30,40,50 → p50=30, p90=50
    rows = [
        ("o/r", "c", "pull_request", w, f"2026-06-10T00:0{i}:00Z", None)
        for i, w in enumerate([10, 20, 30, 40, 50])
    ]
    st = _store(rows)
    out = st.runner_wait_health("o/r", "2026-06-09T00:00:00Z", "2026-06-01T00:00:00Z")
    assert len(out) == 1
    s = out[0]
    assert s.event == "pull_request" and s.p50 == 30.0 and s.p90 == 50.0
    assert s.n == 5 and s.prev_p50 is None


def test_runner_wait_health_prev_window_and_boundary():
    rows = [
        # prior window [prev_since, since): 100,200 → prev_p50=100
        ("o/r", "c", "push", 100.0, "2026-06-05T00:00:00Z", None),
        ("o/r", "c", "push", 200.0, "2026-06-06T00:00:00Z", None),
        # exactly at since → counts as CURRENT
        ("o/r", "c", "push", 10.0, "2026-06-09T00:00:00Z", None),
        ("o/r", "c", "push", 20.0, "2026-06-10T00:00:00Z", None),
    ]
    st = _store(rows)
    out = st.runner_wait_health("o/r", "2026-06-09T00:00:00Z", "2026-06-01T00:00:00Z")
    s = out[0]
    assert s.n == 2 and s.p50 == 10.0 and s.prev_p50 == 100.0


def test_runner_wait_health_sorted_by_event_and_excludes_empty_current():
    rows = [
        ("o/r", "c", "push", 5.0, "2026-06-10T00:00:00Z", None),
        ("o/r", "c", "merge_group", 7.0, "2026-06-10T00:00:00Z", None),
        # this event only has a PRIOR-window row → excluded (no current samples)
        ("o/r", "c", "schedule", 9.0, "2026-06-05T00:00:00Z", None),
    ]
    st = _store(rows)
    out = st.runner_wait_health("o/r", "2026-06-09T00:00:00Z", "2026-06-01T00:00:00Z")
    assert [s.event for s in out] == ["merge_group", "push"]
