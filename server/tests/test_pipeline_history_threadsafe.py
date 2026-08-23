"""HistoryStore must be usable from a worker thread.

pr_population moves ETA enrichment onto a thread (see P1 Task 2); a store that
raises ProgrammingError off the main thread would make that impossible.
"""
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from server.pipeline.history import HistoryStore


def _store(tmp_path) -> HistoryStore:
    p = tmp_path / "ph.db"
    c = sqlite3.connect(str(p))
    c.executescript(
        """
        CREATE TABLE check_durations (repo TEXT, check_name TEXT, event TEXT,
          duration_secs REAL, completed_at TEXT, conclusion TEXT, head_sha TEXT,
          run_attempt INTEGER, started_at TEXT, run_number INTEGER);
        CREATE TABLE runner_waits (repo TEXT, check_name TEXT, event TEXT,
          wait_secs REAL, started_at TEXT, pool TEXT);
        """
    )
    for i, dur in enumerate([100, 110, 120, 130, 140, 150]):
        c.execute(
            "INSERT INTO check_durations VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("o/n", "ci", "pull_request", dur, f"2026-06-2{i}T00:00:00Z",
             "SUCCESS", "sha", 1, f"2026-06-2{i}T00:00:00Z", i),
        )
    c.commit()
    c.close()
    return HistoryStore(str(p))


def test_recent_durations_works_from_a_worker_thread(tmp_path):
    store = _store(tmp_path)
    expected = store.recent_durations("o/n", "ci", "pull_request")
    assert expected, "fixture should produce at least one sample"

    with ThreadPoolExecutor(max_workers=1) as ex:
        got = ex.submit(store.recent_durations, "o/n", "ci", "pull_request").result()

    assert got == expected


def test_concurrent_readers_do_not_interleave(tmp_path):
    """Two threads hammering the same connection must not raise or return garbage."""
    store = _store(tmp_path)
    expected = store.recent_durations("o/n", "ci", "pull_request")

    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [
            ex.submit(store.recent_durations, "o/n", "ci", "pull_request")
            for _ in range(40)
        ]
        results = [f.result() for f in futures]

    assert all(r == expected for r in results)
