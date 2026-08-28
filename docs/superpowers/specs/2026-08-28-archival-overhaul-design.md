# Archival Overhaul — Status-Flip Archive + Cap-Driven Reaper

**Date:** 2026-08-28
**Status:** Approved design, pending implementation plan

## Problem

Archiving a session today is a slow filesystem job forced through a fast-request
pipe. `archive_session()` synchronously (within the HTTP request / daemon RPC):
stops the X11 display, closes the PTY driver (up to 5 s grace), resolves the git
common dir, **physically moves the entire worktree** into `archive_home`
(pruning `node_modules`/`.venv`, potentially a cross-device copy), deregisters
the worktree, dequeues, and persists the DB update.

Observed consequences, all downstream of that one design choice:

- **"Fails, then succeeds later":** big or concurrent moves exceed the ~30 s
  edge→daemon RPC timeout. The daemon finishes the archive anyway; the edge
  returns 503; the UI's error handler restores the session to the list; a later
  refetch finds it archived and it vanishes again.
- **Responsiveness / apparent crashes:** simultaneous archives (auto-archive
  fires in bursts on completion) saturate the small `asyncio.to_thread` pool
  and have historically blocked the event loop long enough to trip the
  health-check watchdog (comment at `orchestrator.py:6932`), restarting the
  daemon — which kills live sessions and presents as a crash.
- **Half-failure states:** out-of-space leaves DB `status='archived'` with the
  worktree still live; SSHFS per-file copy errors require special-casing;
  unarchive can 410 when the archive dir is damaged or missing.
- **Confusing UX:** optimistic removal + error bounce-back + ambiguous toasts.

## Decision

Separate the **state change** from the **physical work** — and then eliminate
the physical work at archive time entirely:

1. **Archive is a pure DB status flip.** No file moves, ever, at archive time.
   The worktree stays where it is.
2. **The existing `max_archived_sessions` cap is the only retention policy.**
   No new age/grace knobs. A worktree lives exactly as long as its archived
   session record; the cap purge that already deletes the oldest records is
   the single place disk is reclaimed.

## Design

### 1. Archive becomes a pure state change

`archive_session()` (orchestrator.py:6880) drops all filesystem work. New
sequence:

1. Stop X11 display (best-effort, as today).
2. Pop in-memory maps (`_drivers`, `_records`, `_last_prompt`,
   `_turn_restart_at`, `_pending_initial_prompt`, `_worktrees`) — same ordering
   discipline as today (pop before driver close).
3. **Spawn** `driver.aclose()` as a fire-and-forget background task. The 5 s
   grace no longer gates any filesystem step, so nothing waits on it.
4. `await sq.remove(session_id)` (still before DB persist so drain cannot
   resurrect a just-archived queued session).
5. One DB UPDATE: `status='archived'`, `archived_by`, `archived_at`,
   `archive_context`. **`archive_root` is left NULL** — its NULL-ness is the
   marker distinguishing new-style archives from legacy ones.
6. Emit the session.terminal event.
7. Fire-and-forget spawn of cap enforcement (see §3) — never awaited in the
   request path.

The request completes in milliseconds and cannot meaningfully time out.
Auto-archive (`_auto_archive_completed`) uses the same path unchanged, so
completion bursts no longer produce thread-pool saturation or watchdog trips.

No git worktree deregistration at archive time — the worktree is still real and
in place; deregistration moves to purge time (§3).

### 2. Unarchive splits on `archive_root`

- **New-style** (`archive_root IS NULL`): flip `status` back, restore the
  record into `_records`; the worktree is already in place. Instant, cannot
  run out of space, cannot 410.
- **Legacy** (`archive_root` set — rows archived before this change): keep the
  existing restore-from-archive-home path (move back, 507/410 error handling)
  so old archives remain unarchivable. Mark it
  `# @deprecated — legacy pre-status-flip archives; remove when no rows with
  archive_root remain` and track removal in an issue.

### 3. Reaper = the existing cap purge, extended

`purge_oldest_archived()` gains worktree deletion: when the cap evicts a
session record it also deletes that session's on-disk directory — the session
`cwd` for new-style rows, the `archive_root` dir for legacy rows — via
`asyncio.to_thread`, throttled to one directory at a time. Git worktree
deregistration (`worktrees.clear_stale_registration`, scoped — never global
prune, per issue #177) happens here too, for new-style rows.

`_enforce_archive_limit()` is no longer awaited anywhere in the archive
request path. It runs in:

- the existing periodic sweep loop (`_archive_limit_loop`), and
- a fire-and-forget spawn after each archive (responsiveness to the cap
  without request-path cost).

Heavy I/O therefore happens only in a background task touching one directory
at a time — never in a request, never in a burst.

### 4. Frontend simplification

- Optimistic removal + 8 s undo toast stay as-is; with a millisecond backend
  the bounce-back window effectively disappears.
- The DaemonUnavailable reconciliation paths in `hooks.ts` stay (daemon
  restarts still exist) but should rarely fire.
- 507 InsufficientStorage handling applies only to legacy unarchive; the
  new-style unarchive path cannot produce it.
- Net UX: Archive click → gone instantly → toast with Undo → appears in
  Archived list. Unarchive click → back instantly.

### 5. Dead code purge (same PR — project norm)

Delete the forward-move half of `archive_move.py`: cross-device guarded copy,
free-space/inode pre-check, `skipped:nospace`, SSHFS `shutil.Error`
per-file handling, and the regenerable-dir pruning that exists solely to make
the move cheaper. Only the legacy restore direction survives, deprecated per
§2. Delete the out-of-space warning branch in `archive_session()` and the
507-producing paths that fed off the forward move. Update or delete
`test_archive_move.py` cases that exercise deleted code.

### 6. Behavior change (accepted)

Archiving no longer frees disk immediately — space is reclaimed when the cap
purges the record. The lever for reclaiming space sooner is lowering
`max_archived_sessions`. (A "purge now" action on the archived list is a
possible follow-up; explicitly out of scope here.)

### 7. Testing

- `test_archive.py`: rewrite assertions — after archive, worktree is untouched
  on disk, `status='archived'`, `archive_root IS NULL`, request path performs
  no `to_thread` filesystem work.
- New: instant unarchive for new-style rows; purge deletes the worktree and
  deregisters it; legacy rows (with `archive_root`) still restore via the old
  path.
- `test_completion_autoarchive.py`: unchanged in spirit (gate/nudge logic is
  untouched).
- No schema changes → no migration, no reconcile-list updates, no Postgres
  suite additions.

## Out of scope

- "Purge now" / manual disk-reclaim UI.
- Any change to completion-bar judgment, nudging, or the auto-archive global
  toggle semantics.
- Removal of the legacy restore path (tracked separately; blocked on no
  remaining `archive_root` rows).

## Expected outcomes

- Archive/unarchive clicks succeed or fail atomically and instantly; the
  "fails then succeeds later" class of behavior is structurally impossible.
- The suspected archival↔responsiveness/crash coupling (watchdog trips from
  burst filesystem work) is removed by construction; verify post-deploy with
  the loop stall tracer.
- Meaningfully less code: the entire cross-device move machinery and its
  error taxonomy are deleted.
