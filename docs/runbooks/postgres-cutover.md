# Postgres cutover runbook

Cuts ccswitch's production database over from SQLite (`/home/tony/.claude-accounts/ccswitch.db`)
to Postgres. This document is a runbook, not a script — every command is meant to be read and
run by a human, in order, with the go/no-go checklist (below) gating the point of no return.

## Hard precondition: the authoritative `--apply` MUST run after writers are stopped

`scripts/ccswitch-sqlite-to-pg.py --apply` copies rows with
`INSERT ... ON CONFLICT DO NOTHING`. That means it only ever **inserts** — it never updates a row
that already exists in the Postgres target. If any ccswitch writer (edge, deploy-controller,
daemon) is still running while `--apply` executes, any row copied early keeps its **old** value
forever: a re-run's `INSERT` for that row's primary key is a no-op conflict, not a refresh. Columns
that mutate in place under normal operation — `sessions.updated_at`, `ci_finding.last_seen_at`,
`automation.last_fired_at`, `app_sessions.revoked_at`, and others — would silently go stale.

Consequence: you may run `--apply` before stopping services as a **throughput pre-warm** (it makes
the authoritative run faster, since most rows are already present and skip on conflict), but that
pre-warm run does **not** count as "the migration." The run that cutover depends on is the one that
happens strictly after step 2 (stop services) below, and `--verify` must be run against that
same post-stop state — not against an earlier pre-warm.

## Timing — budget for downtime

Measured against the test database: **~11.87M rows copied in ~8 minutes, ~20,700 rows/sec**.
`ci_test_result` dominates the row count and keeps growing between measurements, so treat this as
a floor. Budget **8–12 minutes of downtime** for the copy step alone, plus a few minutes for the
stop/verify/start sequence around it. Do the pre-warm run (below) first so the authoritative run
mostly hits `ON CONFLICT DO NOTHING` and finishes near the fast end of that range.

Note `ci_test_result` is retention-scoped by design (`--raw-days`, default 7): the script copies
every failed/error row, plus passed/skipped rows inside the raw window, plus any row with a NULL
`outcome`/`created_ts` — mirroring the live retention job's own delete predicate. It does **not**
copy the full 11M+ history verbatim; do not be alarmed that `ci_test_result` row counts in Postgres
are smaller than SQLite's total.

## Prerequisites

- Tasks 1–4 merged and deployed (schema/model changes, migration script, provisioning script, and
  Postgres-only test suite already exist on this checkout).
- `ccswitch` Postgres role and database provisioned:
  `sudo -u postgres bash scripts/ccswitch-pg-provision.sh '<password>'`
  (idempotent — safe to re-run; creates/updates the `ccswitch` role and `ccswitch` database, owner
  `ccswitch`, and grants `ALL ON SCHEMA public`).
- `CCSWITCH_TEST_PG_URL` pointed at a scratch database and
  `.venv/bin/python -m pytest server/tests/ -q` green with the Postgres-only tests
  (`server/tests/test_postgres_compat.py` and friends) **running**, not skipped.
- A pre-warm dry run and `--apply` already executed against production Postgres (see below) so the
  authoritative run is fast.
- `$PG_URL` exported to the real `postgresql+asyncpg://ccswitch:<password>@<host>/ccswitch` URL for
  this shell.

### Optional: pre-warm (safe to run any time before stopping services)

```bash
PYTHONPATH=. .venv/bin/python scripts/ccswitch-sqlite-to-pg.py --apply --pg-url "$PG_URL"
```

This is safe to run repeatedly while ccswitch is live — it only ever adds rows, never mutates
existing ones — but per the precondition above, it does **not** replace the authoritative run.

## Cutover

1. **Back up SQLite.**
   ```bash
   cp -a /home/tony/.claude-accounts/ccswitch.db /mnt/hdd-storage/ccswitch.db.pre-pg
   ```
   The migration script opens SQLite read-only and never modifies it, so this backup — and the
   original file itself — remain the rollback path throughout.

2. **Stop services — edge and deploy-controller BEFORE the daemon.** Stopping in this order means
   nothing can accept a new write while the daemon is still draining in-flight session state:
   ```bash
   sudo systemctl stop ccswitch.service ccswitch-deploy-controller.service
   sudo systemctl stop ccswitch-daemon.service
   ```

3. **Confirm nothing is live:**
   ```bash
   systemctl is-active ccswitch.service ccswitch-deploy-controller.service ccswitch-daemon.service
   ```
   Expect `inactive` (or `failed`, never `active`) for all three before proceeding.

4. **Migrate — the authoritative run:**
   ```bash
   PYTHONPATH=. .venv/bin/python scripts/ccswitch-sqlite-to-pg.py --apply --pg-url "$PG_URL"
   ```
   Expected, non-alarming output — do not mistake these for failures:
   - A line like `automation_event.session_id: 296 orphaned reference(s) to sessions.id will be
     set NULL (column is nullable, ondelete=SET NULL semantics)`, and a final
     `TOTAL dangling FK values nulled across all tables: ~296`. This is intended: `session_id` is
     `ForeignKey("sessions.id", ondelete="SET NULL")` and nullable — SQLite never enforced that FK,
     so these rows always pointed at deleted sessions; Postgres now stores what the schema always
     specified.
   - `merge_queue_observations` (and any other table the app actively prunes) may show **more**
     rows copied into Postgres than currently exist in SQLite at comparison time, because the copy
     is append-only while the live pruning job deletes from SQLite. Not a discrepancy.
   - A trailing `Advancing identity sequences...` block, one line per single-integer-PK table,
     e.g. `sessions                     sequence sessions_id_seq -> 1842`. This is expected and
     required — see step 5.

5. **Verify — row counts AND sequences, run as a separate invocation** (`--apply` and `--verify`
   are mutually exclusive on one run):
   ```bash
   PYTHONPATH=. .venv/bin/python scripts/ccswitch-sqlite-to-pg.py --verify --pg-url "$PG_URL"
   ```
   Two things must both be true — row counts matching is **not** sufficient on its own:
   - Every table line reads `OK  <table>  sqlite=N  pg=N`. A `DIFF` here, **with services stopped**,
     is a real problem (unlike a `DIFF` seen during a pre-warm run while services were still live
     and the SQLite source was still growing — that case is expected and not a failure signal).
   - The sequence-check block below the row counts reads `OK  <table>.<pk> seq  last_value=X
     max_pk=Y` for every single-integer-PK table, with `last_value >= max_pk`. This step exists
     because copying explicit primary keys does **not** advance a Postgres identity/SERIAL
     sequence — the sequence stays at its initial state while the table fills up around it. A
     cutover where row counts match but sequences were never checked is **not verified**: the
     app's very first `INSERT` without an explicit id (the normal case for a brand-new session,
     event, etc.) would raise `duplicate key value violates unique constraint` against Postgres.
     `--apply` (step 4) already ran `_advance_sequences`, so this should read `OK` across the
     board; a `DIFF` here means stop and investigate before switching the URL.

   Stop here — do not proceed to step 6 — if either check fails.

## Go/no-go checklist

Tick every box before switching `CCSWITCH_DB_URL`. If any item is unchecked or failed, **STOP and
do not switch the URL** — roll back per the section below instead.

- [ ] SQLite backup taken and confirmed present (`ls -la /mnt/hdd-storage/ccswitch.db.pre-pg`).
- [ ] `ccswitch.service`, `ccswitch-deploy-controller.service`, `ccswitch-daemon.service` all
      confirmed `inactive`/`failed` (step 3).
- [ ] Authoritative `--apply` (step 4) completed with no `BLOCKED` `SystemExit` and no unhandled
      exception.
- [ ] `--verify` row counts (step 5) show `OK` for every table.
- [ ] `--verify` sequence checks (step 5) show `OK` for every sequenced table.
- [ ] `$PG_URL` in hand is the real production URL, not the scratch/test database used for the
      dry-run or CI (`CCSWITCH_TEST_PG_URL`).

## Activate the URL

6. Edit `/etc/ccswitch/ccswitch.env`: uncomment the Postgres `CCSWITCH_DB_URL=postgresql+asyncpg://
   ccswitch:<password>@<host>/ccswitch` line and comment out the SQLite one.

7. **Start services** — reverse of the stop order, daemon first so the edge/controller find it
   already up:
   ```bash
   sudo systemctl start ccswitch-daemon.service
   sudo systemctl start ccswitch.service ccswitch-deploy-controller.service
   ```

## Post-cutover verification

Confirm the app actually *works* against Postgres, not merely that the processes started — in
particular, exercise a write path, since the identity-sequence bug (step 5) can only surface on an
INSERT without an explicit id:

1. Health endpoint returns 200:
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/api/health
   ```
2. Dashboard loads in a browser and lists existing sessions/accounts (proves reads work end to
   end, including relationships/FKs).
3. Daemon and edge logs show no `DataError` or `duplicate key`:
   ```bash
   journalctl -u ccswitch-daemon.service -u ccswitch.service -n 200 --no-pager | \
     grep -iE 'dataerror|duplicate key' || echo "clean"
   ```
4. **Exercise a write path** — create a new session (or trigger any action that inserts a row,
   e.g. adding an account or firing an automation) and confirm it succeeds and is visible after a
   page reload. This is the step that actually proves the sequence advance in step 4/5 worked;
   reads alone cannot catch a sequence that was never advanced.

If all four pass, the cutover is complete. If any fails, treat it as inside the rollback window —
see below.

## Rollback

The SQLite file was opened read-only throughout and was never modified by the migration, so
rollback is a config revert, not a data restore:

1. Stop services:
   ```bash
   sudo systemctl stop ccswitch.service ccswitch-deploy-controller.service
   sudo systemctl stop ccswitch-daemon.service
   ```
2. Restore the SQLite `CCSWITCH_DB_URL` line in `/etc/ccswitch/ccswitch.env` (re-comment the
   Postgres line, uncomment the SQLite one).
3. Start services (daemon first, as in step 7).

**Any writes that landed in Postgres after cutover are lost on rollback** — SQLite was frozen at
the moment services stopped in step 2 of Cutover, and rollback simply repoints the app back at
that frozen file. Treat the period immediately after cutover (through the post-cutover
verification above) as the decision window: the longer the app runs on Postgres before a rollback
is called, the more real user data a rollback discards. Decide quickly.

## Known gotchas

- **Stale daemon socket.** `/run/ccswitch/sessiond.sock` survives a `systemctl stop`
  (`RuntimeDirectoryPreserve=yes` on `ccswitch-daemon.service` and
  `ccswitch-deploy-controller.service`) and trips the split-brain guard at `server/app.py:350`.
  Harmless for the real services on restart (the daemon recreates and rebinds it), but if you run
  `pytest` on this host while services are stopped, set
  `CCSWITCH_SESSIOND_SOCKET=/nonexistent/ccswitch-test.sock` first or the test process will refuse
  to start in-process, mistaking the stale socket for a live daemon.
- **`init_db`/`create_all` is a safe no-op on Postgres once tables exist.** Every service start
  runs `create_all`; on Postgres this is additive-only (`CREATE TABLE IF NOT EXISTS`-equivalent)
  and does nothing once the schema is in place. The SQLite-only reconcile/backfill passes are
  correctly skipped by the code's `url.startswith("sqlite")` guard, so they do not run against
  Postgres.
- **`--apply` and `--verify` are mutually exclusive per invocation.** Run apply, then verify, as
  two separate commands (this is enforced — the script raises `SystemExit` if both flags are
  passed together).
- **`--raw-days` defaults to 7.0** and only affects `ci_test_result`'s retention scoping; every
  other table copies in full regardless of this flag. No need to pass it explicitly unless you are
  deliberately changing the raw-result retention window for this migration.
