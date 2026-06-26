from datetime import datetime, timezone
import sqlite3
from server.pipeline.history import HistoryStore, median, percentile


def test_math_matches_plotroom():
    # percentile: sorted[min(len-1, ceil(p*len)-1)]
    assert percentile([1, 2, 3, 4], 0.5) == 2          # ceil(0.5*4)-1 = 1 → 2
    assert percentile([1, 2, 3, 4], 0.9) == 4
    # median: lower median, s[floor((len-1)/2)]
    assert median([3, 1, 2]) == 2
    assert median([4, 1, 3, 2]) == 2                   # floor(3/2)=1 → sorted[1]=2


def _store(tmp_path):
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
    # 6 fresh SUCCESS samples for (o/n, ci, pull_request)
    for i, dur in enumerate([100, 110, 120, 130, 140, 150]):
        c.execute("INSERT INTO check_durations VALUES (?,?,?,?,?,?,?,?,?,?)",
                  ("o/n", "ci", "pull_request", dur, f"2026-06-2{i}T00:00:00Z",
                   "SUCCESS", "sha", 1, f"2026-06-2{i}T00:00:00Z", i))
    c.execute("INSERT INTO check_durations VALUES (?,?,?,?,?,?,?,?,?,?)",
              ("o/n", "ci", "pull_request", 999, "2026-06-20T00:00:00Z", "FAILURE",
               "sha", 1, "2026-06-20T00:00:00Z", 9))  # non-SUCCESS ignored
    for w in [4, 6, 8]:
        c.execute("INSERT INTO runner_waits VALUES (?,?,?,?,?,?)",
                  ("o/n", "ci", "pull_request", w, "2026-06-20T00:00:00Z", "ubuntu"))
    c.commit(); c.close()
    return HistoryStore(str(p))


def test_expected_and_samples_success_only(tmp_path):
    s = _store(tmp_path)
    exp = s.expected("o/n", "ci", "pull_request")
    # p50: percentile([100,110,120,130,140,150], 0.5)
    #   = sorted[min(5, ceil(0.5*6)-1)] = sorted[min(5, ceil(3)-1)] = sorted[min(5, 2)] = sorted[2] = 120
    assert exp is not None and exp.n == 6 and exp.p50 == 120
    assert 999 not in s.samples("o/n", "ci", "pull_request")   # FAILURE excluded
    assert s.expected("o/n", "missing", "pull_request") is None


def test_expected_runner_wait_is_median(tmp_path):
    s = _store(tmp_path)
    assert s.expected_runner_wait("o/n", "ci", "pull_request") == 6
    assert s.expected_runner_wait("o/n", "none", "pull_request") is None


def test_expected_set_window(tmp_path):
    s = _store(tmp_path)
    now = datetime(2026, 6, 26, tzinfo=timezone.utc)
    names = s.expected_set("o/n", "pull_request", now, window_days=14)
    assert "ci" in names
