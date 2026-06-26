import sqlite3
from server.pipeline.history_migrate import migrate_history


def _make_source(path):
    c = sqlite3.connect(path)
    c.executescript(
        """
        CREATE TABLE check_durations (repo TEXT, check_name TEXT, event TEXT,
          duration_secs REAL, completed_at TEXT, conclusion TEXT, head_sha TEXT,
          run_attempt INTEGER, started_at TEXT, run_number INTEGER);
        CREATE TABLE runner_waits (repo TEXT, check_name TEXT, event TEXT,
          wait_secs REAL, started_at TEXT, pool TEXT);
        INSERT INTO check_durations VALUES
          ('o/n','ci','pull_request',120.0,'2026-06-20T00:00:00Z','SUCCESS','sha',1,'2026-06-20T00:00:00Z',1);
        INSERT INTO runner_waits VALUES ('o/n','ci','pull_request',5.0,'2026-06-20T00:00:00Z','ubuntu');
        """
    )
    c.commit(); c.close()


def test_migrate_copies_rows_and_counts(tmp_path):
    src = tmp_path / "src.db"; dest = tmp_path / "pipeline_history.db"
    _make_source(str(src))
    counts = migrate_history(str(src), str(dest))
    assert counts == {"check_durations": 1, "runner_waits": 1}
    d = sqlite3.connect(str(dest))
    assert d.execute("SELECT count(*) FROM check_durations").fetchone()[0] == 1
    assert d.execute("SELECT check_name FROM runner_waits").fetchone()[0] == "ci"


def test_migrate_is_idempotent(tmp_path):
    src = tmp_path / "src.db"; dest = tmp_path / "d.db"
    _make_source(str(src))
    migrate_history(str(src), str(dest))
    counts = migrate_history(str(src), str(dest))  # second run replaces, not appends
    assert counts == {"check_durations": 1, "runner_waits": 1}
    d = sqlite3.connect(str(dest))
    assert d.execute("SELECT count(*) FROM check_durations").fetchone()[0] == 1
