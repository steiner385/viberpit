# Archived-session auto-purge: quick-purge shortcuts + max-archived cap

**Date:** 2026-07-17
**Scope:** backend (`server/`) + frontend (`web/`). One nullable DB column, one settings field,
one new orchestrator method + background sweep, four UI shortcut buttons.
**Related:** [`2026-06-23-bulk-archived-sessions-design.md`](2026-06-23-bulk-archived-sessions-design.md)
(multi-select + batch delete), [`2026-06-30-bulk-delete-progress-design.md`](2026-06-30-bulk-delete-progress-design.md)
(chunked delete + progress). This feature builds on both — it does not replace them.

## Problem

Archived sessions accumulate without bound. Each retains an on-disk working directory under
`archive_dir()/<id>/` (pruned of `node_modules`/`.venv`, but still real disk) plus a DB row. Two
gaps:

1. **No fast way to clear a backlog.** The only bulk tool is manual multi-select. Clearing hundreds
   means select-all-then-delete or shift-click ranges — tedious when the intent is simply "trim the
   old ones."
2. **No retention policy.** Nothing caps the archived count, so disk grows monotonically until a
   human notices and hand-deletes.

## Goals / non-goals

**Goals**
- One-click shortcuts to permanently delete the **oldest** 25 / 50 / 100 / 200 archived sessions.
- A configurable **max archived sessions** cap; when exceeded, the **oldest** are auto-purged.
- Enforcement that survives a *lowered* limit even with no new archive event.

**Non-goals (YAGNI)**
- No per-account or per-repo limits.
- No age-based TTL ("purge older than 30 days").
- No scheduled / quiet-hours purge windows.
- No new backend endpoint for the shortcuts — they reuse the existing batch-delete pipeline.

## Behavior

### Quick-purge shortcuts
- Four buttons — **25 / 50 / 100 / 200** — in the ArchivedManager toolbar, labelled "Purge oldest".
- "Oldest" = ascending `archivedAt` (the field the archived list already sorts on).
- A button is **disabled when fewer than N archived sessions exist** (e.g. "100" is disabled with
  40 archived), with a tooltip explaining why. This prevents "Purge oldest 200" from silently
  nuking a small list, and keeps each button's meaning literal.
- Clicking opens the **existing** bulk `PermanentDeleteDialog` pre-loaded with the oldest-N snapshot,
  so the user gets the same confirm step, chunked progress bar, and cancel as manual bulk delete.

### Max-archived cap
- A single nullable integer setting, `maxArchivedSessions`. **`null` = unlimited** (default).
- When the archived count exceeds the cap, the `count − cap` **oldest** archived sessions are
  permanently deleted (same destructive semantics as `permanent_delete`).
- Enforced at **two** trigger points:
  - **On archive** — right after `archive_session()` commits, best-effort.
  - **Periodic sweep** — every 5 min, so a limit *lowered* via Settings takes effect without waiting
    for the next archive.

## Approach

The two features share one core operation — *select the oldest archived sessions and permanently
delete them* — so both route through a single new orchestrator method rather than parallel code
paths (constitution VI / `eliminating-parallel-implementations`).

- **Shortcuts** need no backend change: the frontend already holds the full archived list
  (`useArchivedSessions`), so it sorts oldest-first client-side, takes N, and feeds the IDs into the
  existing `useBulkPermanentDeleteSessions()` → `POST /sessions/batch/permanent-delete`.
- **Cap enforcement** lives entirely **inside the daemon orchestrator** (called from
  `archive_session` and a background loop), so it needs **no** RemoteOrchestrator proxy or RPC
  registration — the API layer never calls it directly.

### Why not alternatives
- **A dedicated `POST /sessions/purge-oldest?n=` endpoint for the shortcuts** — would duplicate the
  best-effort batch-delete loop and lose the existing progress/cancel UX. Rejected: client-side
  selection + existing endpoint is strictly less surface.
- **On-archive enforcement only** — simpler, but a newly-lowered limit wouldn't apply until the next
  archive happened. The periodic sweep closes that gap cheaply (a COUNT + bounded LIMIT select).
- **"Keep newest N" reinterpretation of the buttons** — considered; rejected in favour of literal
  "purge oldest N" so the buttons and the cap share the same oldest-first mental model.

## API / data model changes

### DB — `AppSettings` (`server/db/models.py`)
```python
max_archived_sessions: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
```
**Nullable is load-bearing:** `init_db` backfills missing nullable columns on the dev SQLite path
without an ALTER, so no migration is required (`create_all` never ALTERs; a NOT NULL column would
need Alembic — see CLAUDE.md "Dev SQLite"). `null` also *is* the "unlimited" default, so the schema
default and the semantic default coincide.

### Settings API (`server/api/settings.py`)
- `SettingsBody` gains `maxArchivedSessions: int | None = None`.
- `_to_dict` returns `"maxArchivedSessions": s.max_archived_sessions`.
- PUT semantics (avoids the "can't distinguish absent from null" trap of the optional-field pattern):
  - field **omitted** (`None`) → unchanged.
  - value **≥ 1** → set, clamped to `_MAX_ARCHIVED_CEILING` (100 000) against fat-fingering.
  - value **≤ 0** → cleared to `null` (unlimited). The UI sends `0` to mean "unlimited"; it never
    sends `null`.

### Orchestrator (`server/sessions/orchestrator.py`) — no RPC/remote changes
- `purge_oldest_archived(count) -> list[str]` — selects the `count` oldest archived rows ordered by
  `coalesce(archived_at, created_at)` ascending (null-safe: legacy rows with no `archived_at` fall
  back to `created_at`), deletes each via the existing `permanent_delete`, returns the IDs actually
  deleted. Best-effort; `count <= 0` no-ops.
- `_read_max_archived_sessions() -> int | None` — reads the singleton; returns the cap (≥1) or
  `None` (unlimited / no row / read error).
- `_enforce_archive_limit() -> int` — reads the cap, counts archived, purges `count − cap` oldest
  when over. No-op when unset or under. Returns the number deleted.
- `_archive_limit_loop()` — sweep every `_ARCHIVE_LIMIT_SWEEP_INTERVAL` (300 s); sleeps first so it
  never races `rehydrate()` at boot. Registered in `start()` as `self._archive_limit_task`, cancelled
  in `stop()`.
- `archive_session()` calls `_enforce_archive_limit()` at its tail, wrapped so a purge failure never
  fails the archive that triggered it. No recursion risk: the sessions it purges are already
  `archived` (not in `_records`/`_drivers`), so `permanent_delete` skips its archive-then-delete path;
  and the just-archived session has the newest `archived_at`, so it can never be selected as oldest.

## Frontend

- **Type** (`web/src/api/hooks.ts`): `AppSettings` gains `maxArchivedSessions: number | null`.
- **Settings field** (`components/settings/GeneralSection.tsx`): a numeric input under an "Archived
  sessions" heading; blank = unlimited (sent as `0`), remounts from server value via the existing
  `key='loaded'` idiom.
- **Shortcuts** (`components/ArchivedManager.tsx`): a `QuickPurgeBar` (buttons `[25,50,100,200]`,
  each disabled when `total < n` or a delete is running) + an `openQuickPurge(n)` handler that sorts
  `archivedSessions` by `archivedAt` (ISO string → lexical sort is chronological), slices N, and
  reuses the existing `bulkSnapshot` + bulk `PermanentDeleteDialog` flow.

## Data flow

**Shortcut:** click N → sort archived oldest-first → slice N → snapshot `{ids, names}` → open bulk
dialog → confirm → existing chunked `useBulkPermanentDeleteSessions` (progress + cancel) → summary.

**Cap (on archive):** `archive_session` commits → `_enforce_archive_limit` → read cap → count
archived → over? → `purge_oldest_archived(excess)` → per-id `permanent_delete`.

**Cap (sweep):** every 300 s → same `_enforce_archive_limit`.

## Error / edge handling

- Cap unset (`null`) / no settings row / settings read error → enforcement is a no-op (unlimited).
- Enforcement failure never propagates out of `archive_session` (best-effort try/except).
- `purge_oldest_archived` is best-effort per id — one failed delete is logged and skipped.
- Sweep loop swallows per-tick exceptions; skips a tick while `_shutting_down`.
- Shortcut button for N > available is disabled, so the handler's empty-slice guard is belt-and-braces.
- Absurd cap values are clamped server-side, so a bad PUT can't wedge enforcement.

## Rollout strategy

- **No migration / no data backfill.** New column is nullable and defaults to `null`; `init_db`
  backfills it on the dev path. Prod (Postgres) picks it up via the same nullable-add path.
- **Default is inert.** `maxArchivedSessions = null` ⇒ enforcement no-ops, the sweep runs but
  early-returns, and the on-archive hook does nothing. **No existing archived session is deleted
  until a user opts in** by setting a cap. This is deliberate: auto-deletion of user data must be
  opt-in.
- **Additive UI.** The shortcut buttons and settings field are new controls; nothing existing moves
  or changes behavior.
- **Ship in one PR** — the change is small and cohesive (backend + settings + UI), gated on the
  existing CI checks (`lint-and-test` = ruff + mypy + pytest; `frontend` = tsc + eslint + vitest).
- **Reversibility.** Setting the cap back to blank (→ `0` → `null`) restores unlimited immediately;
  the periodic sweep stops purging on its next tick. No schema rollback needed.

## Testing

**Server (`pytest`)**
- `test_archive_purge.py` (new): `purge_oldest_archived` deletes oldest-first / no-ops on 0 /
  null-`archived_at` falls back to `created_at`; `_enforce_archive_limit` trims to cap, no-ops when
  unset and when under; `archive_session` enforces the cap on archive (fresh survives, oldest purged).
- `test_settings.py`: `maxArchivedSessions` GET default `null`, PUT round-trip, `0` clears to
  unlimited, absurd value clamps to the ceiling.
- Regression: `test_archive.py`, `test_archived_at.py`, and the RPC/remote breadth suites stay green
  (confirms no new RPC surface was needed).

**Web (`vitest` + `tsc` + `eslint`)**
- `QuickPurgeBar` disabled-state by count; clicking selects the correct oldest-N and opens the bulk
  dialog.
- Settings field: blank→save sends `0`; a number round-trips.
- Keep existing `ArchivedManager` / settings tests green.
