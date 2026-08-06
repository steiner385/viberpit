# Recoverable queue-start failures re-enqueue instead of ending

**Date:** 2026-07-22
**Status:** Approved design — ready for implementation plan
**Area:** `server/sessions/orchestrator.py` (session priority queue)

## Problem

Since PR #628 ("global max-active-sessions cap with server-side priority queue"),
sessions created past the global active cap (default 5,
`DEFAULT_MAX_ACTIVE_SESSIONS`) are persisted as `status="queued"` rows and started
later by `_maybe_start_queued()` as slots free up (on session close, on status
change, and on daemon rehydrate — `queue-drain-on-rehydrate`).

`_maybe_start_queued()` has a single, unconditional failure policy: **any**
exception from `_spawn_session()` marks the session `ended`
(`orchestrator.py:855-888`). The documented rationale is correct as far as it goes —
a failed-to-start session must not hold a cap slot forever and starve the queue —
but it does not distinguish *why* the start failed.

In practice the dominant failure is **recoverable**: the session's account needs
re-authentication (a 401 from a dead/expired token) or is quota-quarantined (429).
When that happens, a perfectly valid queued session is converted to the terminal
`ended` state and the queued work is silently lost. The user re-authenticates the
account, but the session is already gone — they see a stream of "Ended" sessions in
the Done group with no indication they were killed by a transient auth problem.

Observed live (2026-07-22): active count sat exactly at the cap of 5 with one
session queued; the most recent `ended` row was a "New Session" on
`tstein_ibaset.com` — the account the UI flagged as needing re-authentication.

### Latent leak (fixed as a side effect)

`_spawn_session()` creates a git worktree (`self._worktrees[sid]`) and an X11
display **before** `driver.start()`. On a `driver.start()` failure it pops the
in-memory record and re-raises, but does **not** tear down the worktree or display.
The current `ended` path doesn't run `close_session` teardown either, so those
resources already leak today. Any retry design that recreates a worktree per
attempt would multiply the leak — directly the inode-exhaustion failure mode this
project has hit before. Cleanup-before-retry is therefore a hard requirement.

## Goals

- A queued session that fails to start for a **recoverable** reason (auth, quota,
  no eligible account, or an unclassified transient error) is **re-enqueued**
  (`status="queued"`), not ended.
- Recoverable failures do not hot-loop the drain or churn worktrees.
- A genuinely **permanent** failure (bad branch/worktree, missing repo) still ends
  the session, exactly as today.
- No cap slot is held by a re-enqueued session (`queued` is already in
  `NON_ACTIVE_SESSION_STATUSES`).
- No resource leaks across retries.

## Non-goals

- **No new session status.** `queued` is reused. A session blocked on re-auth looks
  identical to one merely waiting for a free slot. Accepted trade-off (chosen to
  avoid fanning out a new status across `statusTier`, nav, and ~15 test files).
  *Future enhancement, out of scope:* surface a "waiting on account re-auth" reason
  on the queue row/position API.
- **Deploy-restore recovery is untouched.** The `_resume_recovery` / `gaveUp`
  ("couldn't be recovered — open to retry or archive") banner is a separate path
  (`restore_deploy` → `_recover_resume_failures_once` → `resume_recovery_status`).
  It already retries rather than ending and shares only the root cause (dead
  account), not the code path.

## Design

### 1. Failure classification

In `_maybe_start_queued()`'s `except` branch, classify the caught exception:

- **Permanent → `ended`** (unchanged behavior):
  - `BranchSwitchError` / `worktrees.WorktreeError` (bad branch / worktree creation
    failed)
  - missing/invalid repo or cwd config errors
  These do not self-heal on retry; terminating is correct.
- **Recoverable → re-enqueue** (everything else):
  - auth 401 / account `needs-reauth`
  - quota 429
  - `AllAccountsBusy` (no eligible account right now)
  - any other unexpected/transient exception (defaults to recoverable; bounded by
    the attempt cap in §4)

Classification reuses the existing failure taxonomy where possible (the same
distinctions `RecoveryState` / `_handle_recoverable_failure` already draw for
live-turn 401/429). The exact exception types raised by `_spawn_session` /
`driver.start()` on a start-time 401/429 will be pinned down during implementation
(a dedicated classifier helper, e.g. `_classify_start_failure(exc) -> Literal[...]`,
keeps the drain loop readable and unit-testable).

### 2. Account-aware drain gating

The drain loop skips work it knows will fail, cheaply:

- **Before** attempting `_spawn_session` for a queued entry, if the entry has a
  pinned account that is currently quarantined (`self._recovery.quarantined(now=…)`)
  or flagged `needs-reauth` in the ledger, **skip** it: leave it queued, advance to
  the next entry. No worktree is created for a spawn we expect to fail.
- **On** a recoverable auth/quota start failure, quarantine the account
  (`QUARANTINE_401` / `QUARANTINE_429`) and mark it `needs-reauth`
  (`_mark_needs_reauth`) before re-enqueueing. This ensures the *first* failure
  trips the gate, so every subsequent drain skips that session without churn until
  the account is re-authenticated.
- Auto-pick sessions (`account=None`) already flow through
  `select_account(exclude=quarantined)`, which raises `AllAccountsBusy` when nothing
  is eligible → recoverable → re-enqueue.

### 3. Clean re-enqueue helper

A new helper `_reenqueue_failed_start(sid, params)` performs, in order:

1. Tear down any partial resources created during the failed `_spawn_session`:
   worktree (`self._worktrees.pop(sid, None)` + remove on disk) and X11 display,
   mirroring `close_session`'s teardown. (Also repairs the pre-existing leak.)
2. Set the in-memory `SessionRecord` and the DB `sessions` row back to
   `status="queued"`.
3. Re-add to the `SessionQueue` table via `sq.enqueue(sid, params)`, which places
   the entry at the tail (new highest priority number) so a repeatedly-failing head
   does not starve the rest of the queue.

`params` is already in scope in the drain loop (it was popped from the queue entry).

### 4. Same-pass guard and attempt cap

Two independent guards prevent runaway behavior:

- **Same-pass guard:** track session ids attempted within a single drain pass in a
  local set. If `next_entry()` returns an already-attempted id (the case when a
  re-enqueued session is the only/highest-priority entry), `break` out of the pass.
  Without this, re-enqueueing the sole queued session tight-loops inside one drain
  call.
- **Attempt cap (non-account failures only):** an in-memory
  `self._queue_start_attempts: dict[str, int]`, mirroring `_resume_recovery`.
  - Account-gated recoverable failures (auth / quota / all-busy) are **not** capped.
    They self-heal on re-auth and wait indefinitely as an ordinary queued session —
    the intended behavior.
  - Any *other* (unclassified transient) failure increments the counter. After
    `MAX_QUEUE_START_ATTEMPTS` (default **5**) the session finally goes `ended`.
    This bounds a genuinely-broken-but-not-config session so it cannot park forever.
  - The counter is cleared on successful start and when the session goes terminal.
  - In-memory only: a daemon restart resets attempts, which is acceptable (a restart
    is a fresh chance).

### 5. Control flow (drain loop, revised)

```
async with self._queue_drain_lock:
    attempted: set[str] = set()
    while active_count < max_active:
        entry = next_entry()
        if entry is None: break
        sid = entry.session_id
        if sid in attempted: break            # same-pass guard
        attempted.add(sid)

        if account_pinned_and_blocked(entry):  # §2 cheap gate
            continue                            # leave queued, try next

        sq.remove(sid)
        in_mem = records.get(sid)
        if in_mem is None or in_mem.status != "queued":
            continue                            # stale entry (concurrent close/archive)
        records.pop(sid)                        # spawn re-inserts as active
        try:
            await _spawn_session(sid, preexisting_row=True, **params)
            started += 1
            queue_start_attempts.pop(sid, None)
        except Exception as exc:
            kind = classify_start_failure(exc)   # §1
            if kind == "permanent":
                mark_ended(sid, params)          # today's behavior
            elif kind in ("auth", "quota"):
                quarantine + mark_needs_reauth(account)   # §2
                await reenqueue_failed_start(sid, params) # §3
            elif kind == "all_busy":
                await reenqueue_failed_start(sid, params)
            else:  # other transient
                n = queue_start_attempts.get(sid, 0) + 1
                if n >= MAX_QUEUE_START_ATTEMPTS:
                    mark_ended(sid, params)
                    queue_start_attempts.pop(sid, None)
                else:
                    queue_start_attempts[sid] = n
                    await reenqueue_failed_start(sid, params)
```

(The existing "stale entry" skip and the `preexisting_row=True` re-insertion in
`_spawn_session` are preserved.)

## Testing

- **Recoverable auth:** queued session, `_spawn_session` raises a start-time 401 →
  session stays `queued`, re-added to `SessionQueue`, account quarantined +
  `needs-reauth`, worktree/X11 cleaned up (no leak), record not `ended`.
- **Drain gating:** with the account quarantined, a subsequent drain **skips** the
  session (no `_spawn_session` call) and starts the next eligible entry instead.
- **Self-heal:** clearing the quarantine / re-auth lets the next drain start the
  session normally.
- **All-accounts-busy:** `AllAccountsBusy` → re-enqueue, uncapped.
- **Permanent:** `WorktreeError` / `BranchSwitchError` → `ended` (unchanged).
- **Attempt cap:** repeated non-account transient failure → re-enqueued up to the
  cap, then `ended`; `_queue_start_attempts` cleared.
- **Same-pass guard:** a single re-enqueued session does not tight-loop within one
  drain pass.
- **Regression:** existing `_maybe_start_queued` cap / starvation / stale-entry
  tests remain green.

## Rollout / risk

- Pure server-side behavior change within `_maybe_start_queued`; no schema change
  (`_queue_start_attempts` is in-memory), no new status, no frontend change.
- Worst case of the broad classification (an unknown exception re-enqueues instead
  of ending) is bounded by the attempt cap, so a doomed start retries at most
  `MAX_QUEUE_START_ATTEMPTS` times before reaching the same terminal `ended` state
  it reaches today.
