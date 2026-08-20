#!/usr/bin/env python3
"""ONE-SHOT compaction of ccswitch.db. Throwaway — delete after sub-project 1b.

Drains the backlog the leak fixes (Tasks 1-4) prevent from recurring:
  1. delete expired/revoked app_sessions      (~2.16M rows)
  2. dedupe ci_test_result by (run_id, suite, test_id), keeping the highest id
  3. fold passed/skipped rows older than --raw-days into ci_test_daily
  4. create the unique indexes create_all cannot add to an existing table
  5. VACUUM to return the freed pages to the filesystem

Run ONLY with ccswitch.service and ccswitch-daemon.service stopped.
"""
import argparse
import sqlite3
import time

DB = "/home/tony/.claude-accounts/ccswitch.db"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--raw-days", type=float, default=7.0)
    ap.add_argument("--apply", action="store_true",
                    help="without this, only report what would change")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.execute("PRAGMA busy_timeout=30000")
    horizon = time.time() - args.raw_days * 86400.0

    def count(sql: str, *p) -> int:
        return con.execute(sql, p).fetchone()[0]

    print("expired app_sessions :", count(
        "SELECT COUNT(*) FROM app_sessions "
        "WHERE revoked_at IS NOT NULL OR expires_at < datetime('now')"))
    print("duplicate test rows  :", count(
        "SELECT COUNT(*) - COUNT(DISTINCT run_id||'|'||COALESCE(suite,'')||'|'||test_id) "
        "FROM ci_test_result"))
    print("foldable test rows   :", count(
        "SELECT COUNT(*) FROM ci_test_result "
        "WHERE outcome NOT IN ('failed', 'error') AND created_ts < ?", horizon))
    if not args.apply:
        print("\ndry run — re-run with --apply")
        return

    con.execute("DELETE FROM app_sessions "
                "WHERE revoked_at IS NOT NULL OR expires_at < datetime('now')")
    con.commit()

    # Index the identity lookup the auth reuse query runs on every authenticated
    # request. Placed AFTER the expired-session delete above so it indexes the
    # small remaining live set, not the full ~2.2M-row backlog.
    con.execute("CREATE INDEX IF NOT EXISTS ix_app_sessions_identity_expires "
                "ON app_sessions (identity, expires_at)")
    con.commit()

    # Keep the highest id per (run_id, suite, test_id) — ingest appends, so the
    # highest id is the last attempt, matching Task 1's keep-last rule.
    con.execute("""
        DELETE FROM ci_test_result WHERE id NOT IN (
            SELECT MAX(id) FROM ci_test_result
            GROUP BY run_id, COALESCE(suite,''), test_id)
    """)
    con.commit()

    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_ci_test_result_run_suite_test "
                "ON ci_test_result (run_id, suite, test_id)")
    con.commit()

    con.execute("""
        CREATE TABLE IF NOT EXISTS ci_test_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo VARCHAR, test_id VARCHAR, day VARCHAR,
            passed INTEGER, failed INTEGER, skipped INTEGER)
    """)
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_ci_test_daily "
                "ON ci_test_daily (repo, test_id, day)")
    con.commit()

    # ON CONFLICT upsert: by the time this script runs, the app's own
    # _rollup_passed may already have folded some of these same (repo, test_id,
    # day) buckets (it fires on the first _prune tick after deploy). A plain
    # INSERT would collide with uq_ci_test_daily and abort the script AFTER the
    # app_sessions delete and the dedupe delete above have already committed.
    con.execute("""
        INSERT INTO ci_test_daily (repo, test_id, day, passed, failed, skipped)
        SELECT repo, test_id, strftime('%Y-%m-%d', created_ts, 'unixepoch'),
               SUM(outcome = 'passed'), 0, SUM(outcome = 'skipped')
        FROM ci_test_result
        WHERE outcome NOT IN ('failed', 'error') AND created_ts < ?
        GROUP BY repo, test_id, strftime('%Y-%m-%d', created_ts, 'unixepoch')
        ON CONFLICT(repo, test_id, day) DO UPDATE SET
            passed = passed + excluded.passed,
            skipped = skipped + excluded.skipped
    """, (horizon,))
    con.execute("DELETE FROM ci_test_result "
                "WHERE outcome NOT IN ('failed', 'error') AND created_ts < ?", (horizon,))
    con.commit()

    print("vacuuming (needs free space equal to the final DB size)…")
    con.execute("VACUUM")
    con.close()
    print("done")


if __name__ == "__main__":
    main()
