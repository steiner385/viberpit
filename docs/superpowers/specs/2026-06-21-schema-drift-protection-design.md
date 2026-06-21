# Schema-Drift Protection — Design

**Date:** 2026-06-21
**Status:** Approved (design) — pending implementation plan

## Problem

The live deployment runs **SQLite + `_reconcile_sqlite_columns`** (not Alembic — nothing in `init_db`'s startup path or `auto-deploy.sh` runs `alembic upgrade`). On startup, `create_all` makes absent *tables* and `_reconcile_sqlite_columns` `ALTER TABLE ADD COLUMN`s any *columns* a pre-existing table is missing.

Reconcile can add nullable columns bare, and NOT NULL columns **only when it can synthesize a DEFAULT** to backfill existing rows (`_ddl_default_literal`). A NOT NULL column whose default is non-static (a callable/server default) is skipped + logged — so an old populated DB never gets it, and every query selecting it raises `no such column`. That is exactly the 2026-06-21 incident (`sessions.name_is_auto`): the auto-deploy shipped `master`, whose model expected a column the live SQLite DB lacked, and every session read threw → the UI showed an empty list (it looked like data loss; the data was intact).

Both the CI tests and the deploy gate build their DB **fresh** via `create_all` (full current schema), so they are blind to drift against an *old, populated* DB — the only place this bug lives.

**Live landmine today:** `Session.tool_allowlist = mapped_column(JSON, nullable=False, default=list)` — NOT NULL with a *callable* default. `_ddl_default_literal` returns `None` for it, so reconcile cannot add it to an old DB. It avoids causing an incident only because the column already exists in the prod DB.

## Goal

Detect reconcile-incompatible schema changes **before merge** (CI) and **before a bad deploy serves traffic** (deploy-time + runtime guard), and close the current `list`/`dict`-default gap. Defense in depth across three layers: pre-merge CI, fail-fast startup, and auto-deploy health-check + rollback.

## Approach

Make `_ddl_default_literal` the **single source of truth** for "can reconcile synthesize this column?" Both the runtime reconcile and the CI coverage test consult it, so what CI enforces and what the deploy actually does can never diverge.

Rejected alternatives:
- **Golden-schema-snapshot diff** — noisy (every legitimate schema change churns the snapshot file) and doesn't test *reconcilability*, only equality.
- **`alembic check` / autogenerate-diff** — guards the Postgres migration path, which is not in the live SQLite deploy path; forces every change through Alembic even though the deploy uses reconcile.

## Components

### 1. Reconcile enhancement (close the gap)
Extend `_ddl_default_literal` in `server/db/__init__.py` to synthesize literals for the common **callable** defaults (detected by evaluating `default.arg(None)` — SQLAlchemy's `CallableColumnDefault` passes an exec context, and `None` suffices for these):
- result `== []` → `'[]'`, result `== {}` → `'{}'` (valid SQLite defaults for a JSON-as-TEXT column; SQLAlchemy deserializes `'[]'`→`[]`).
- result is a `datetime` (e.g. `default=datetime.utcnow`) → a **constant** literal `'1970-01-01 00:00:00'`. (SQLite rejects `CURRENT_TIMESTAMP` as a non-constant DEFAULT in `ALTER TABLE ADD COLUMN`; an epoch sentinel is honest — a row predating the column has an unknown/ancient timestamp — and these columns realistically never go missing, so the value is academic, it only satisfies reconcilability.)

This is required, not optional: introspection shows **three** NOT NULL columns with callable defaults today — `tool_allowlist` (`list`→`[]`) **and** `created_at`/`updated_at` (`datetime.utcnow`→`CURRENT_TIMESTAMP`). Without the datetime case the coverage test (component 2) would flag the timestamp columns. After this enhancement, every current column is reconcilable with **no exemptions needed**. Scalar handling (bool/int/float/str) is unchanged. A genuinely non-synthesizable callable still returns `None`.

### 2. CI test — reconcile coverage
`server/tests/test_schema_reconcile_coverage.py`. Iterate every `Base.metadata` table and column; assert each column is reconcilable:

```
column.nullable
  OR column.primary_key                       # part of CREATE TABLE, never ALTER-added
  OR _ddl_default_literal(column) is not None
  OR column.name in RECONCILE_EXEMPT
```

On failure, the message names the offending `table.column` and the remedy: "give it a scalar/list/dict default, or add it via a real migration and list it in `RECONCILE_EXEMPT`."

`RECONCILE_EXEMPT: set[str]` (keyed `"table.column"`) is the documented escape hatch for columns intentionally handled by a real migration rather than reconcile. Starts empty.

### 3. CI test — baseline upgrade (end-to-end)
`server/tests/test_schema_upgrade_from_baseline.py` + `server/tests/fixtures/legacy_sessions.sql`.

The fixture is an *old* `sessions` table — the minimal early columns (`id, name, account_name, status, cwd`) — with one seed row. The test:
1. Creates a SQLite DB from the fixture (with the seed row).
2. Runs `init_db(db_url=<that file>)` (which runs reconcile).
3. Asserts **every** current `Session` model column now exists in the DB.
4. Asserts the seed row still `SELECT *`s without error (round-trip against real data).

This reproduces the incident's shape and proves reconcile bridges old→current. The fixture stays intentionally ancient (worst case), so it never needs updating as the model grows.

### 4. Startup self-check (fail fast, loud)
In `init_db`, after `_reconcile_sqlite_columns`, introspect each table and compare model columns to actual DB columns. If any model column is still missing, raise `SchemaDriftError(table, missing_columns)` with a clear message. A drifted server **refuses to start** instead of silently serving an empty list. New: `class SchemaDriftError(RuntimeError)` in `server/db/__init__.py`.

The self-check runs only on the SQLite path (same guard as reconcile). It is the signal the deploy health-check keys on (a drifted deploy crash-loops loudly rather than serving empty).

### 5. `/healthz` endpoint
Unauthenticated, minimal route (no `current_identity`) registered on the app. It runs a schema-touching query — `SELECT <all Session model columns> FROM sessions LIMIT 1` — inside a try/except:
- success → `200 {"status":"ok"}`
- any DB error → `500 {"status":"schema_error","detail":<str>}`

This gives `auto-deploy.sh` a clean liveness + schema-sanity signal without needing a Tailscale identity. It is intentionally distinct from the existing authenticated metrics/health surfaces.

### 6. `auto-deploy.sh` health-check + rollback
After `sudo systemctl restart ccswitch.service`, poll `http://$CCSWITCH_HOST:$CCSWITCH_PORT/healthz` for up to ~30 s (e.g. 15 × 2 s). On the first `200`, deploy succeeds. If it never returns `200`:
1. `git reset --hard "$LOCAL"` (the pre-pull SHA, already captured).
2. `( cd web && npm run build )`.
3. `sudo systemctl restart ccswitch.service`.
4. `echo "[deploy] ROLLED BACK to ${LOCAL:0:9}" >&2; exit 1`.

A bad deploy auto-reverts to last-good instead of leaving a broken server up. If the rollback build/restart itself fails, `set -euo pipefail` aborts loudly (the broken-but-visible state is preferable to a silent half-rollback).

## Data flow

```
PR / push ─► ci.yml ─► pytest server/tests/ ─► coverage test + baseline-upgrade test
                                                 (drift → red PR)

merge ─► auto-deploy.sh:
   git pull --ff-only
   web build  ─┐
   pytest server/tests  ─┤ gate (incl. the two drift tests) — abort before restart on red
   systemctl restart ────┘
   poll /healthz ──► 200? done : rollback to $LOCAL

runtime ─► init_db: create_all → reconcile → startup self-check (SchemaDriftError if drift)
        ─► /healthz: schema-touching query → 200 / 500
```

## Error handling
- `SchemaDriftError` on startup: process exits non-zero → systemd Restart=on-failure crash-loops (visible) → `/healthz` never 200s → auto-deploy rolls back.
- `/healthz` catches any DB exception and returns 500 (never raises out of the handler).
- Rollback is bounded by the poll timeout; a failed rollback aborts loudly rather than silently.

## Testing
- Coverage test + baseline-upgrade test are the primary deliverables (and run in CI + the deploy gate).
- Unit test for the reconcile `list`/`dict`-default enhancement (an old DB missing a NOT NULL JSON column gains it, backfilled to `[]`).
- Startup self-check test: run `init_db` against a DB with a deliberately dropped/altered column that reconcile cannot fix (a `RECONCILE_EXEMPT`-style case) and assert `SchemaDriftError` is raised.
- `/healthz` test: `200` on a healthy DB; `500` when a model column is missing.
- `auto-deploy.sh` is shell; covered by the `/healthz` contract test + manual verification (no bash test harness in repo).

## Out of scope (YAGNI)
- Alembic autogenerate-check / migrating the deploy to Alembic.
- Postgres-specific drift handling (deploy is SQLite).
- Column *renames*/*drops* (reconcile only adds; a rename is a model+migration concern, not this gap).
- A general-purpose `/health` dashboard (the existing authenticated metrics surface stays as-is).
