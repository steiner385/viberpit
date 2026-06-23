# Idle-account token durability — design

**Date:** 2026-06-23
**Status:** approved (proceed autonomously)
**Origin:** post-incident diagnosis — three accounts shown as `needs-reauth` one
morning. Two had simply auto-recovered (expired access tokens refreshed on the
first morning poll); `kindash` was genuinely dead. Its `*.cred.json` froze at
19:01 and never updated again, after which every refresh returned `invalid_grant`
→ permanent `needs-reauth`.

## Problem

Anthropic OAuth refresh tokens are **single-use**: every successful refresh
rotates the token and invalidates the one presented. ccswitch refreshes tokens
from **three processes** that can run concurrently against the same account:

- the **web daemon** (the usage poller, in remote mode),
- the **edge** process (in-process mode),
- the standalone **`ccswitch` CLI** (`launcher.py` / `cli.py` / `runner.py`).

The refresh+persist critical section in `ccswitch.accounts` is guarded only by a
per-process `threading.Lock` (`_account_lock`). It does **not** span processes.
When two processes refresh the same account at once, both present the same
single-use token; the first rotates it, the second receives `invalid_grant` and
flips the account to `needs-reauth` — even though a valid rotated token now sits
on disk. This is the most likely cause of the `kindash` failure and is an
instance of the known "remote-mode path asymmetry".

A secondary gap: idle accounts only stay fresh while the poller runs. A
usage-endpoint throttle puts an account into the poller's backoff cooldown, and
during cooldown the token-refresh that piggybacks on the usage fetch is skipped.
More importantly, a daemon that is down or wedged (as during the 23:00 forced
restart) refreshes nothing, so idle access tokens silently lapse and present as
broken on the next dashboard open.

A rejected single-use refresh token **cannot** be programmatically un-rejected —
so the goal is **prevention and durability**, not reviving a dead token.

## Goals

1. **Prevent lost rotations** — make refresh+persist correct across processes.
2. **Keep idle tokens fresh** — refresh idle accounts independent of usage-fetch
   health, including immediately on daemon startup.

Non-goals: UX/observability changes (deferred); reviving already-dead tokens
(impossible).

## Design

### Thread 1 — cross-process refresh lock (`ccswitch.accounts`)

Add `_refresh_guard(name)`, a context manager that nests a **cross-process
advisory file lock** (`fcntl.flock(LOCK_EX)` on `store/<name>.refresh.lock`)
*inside* the existing per-process `threading.Lock`:

- `threading.Lock` serialises threads within a process (cheap, and required
  because `flock` ownership is per-process, not per-thread).
- `flock` serialises the refresh across the daemon, edge, and CLI processes.

Replace the three refresh+persist critical sections that currently use
`with _account_lock(name):` with `with _refresh_guard(name):`:

- `ensure_valid_token` (the primary refresh path),
- the 401-survived-refresh path in `usage_for`,
- `sync_back`'s slot read-modify-write.

The existing **re-read inside the lock** stays: the race loser now blocks on the
`flock`, then re-reads the freshly-persisted rotated token and returns it instead
of refreshing again — turning a race into a no-op. As cheap belt-and-suspenders,
before `usage_for` flips an account to `needs-reauth` on `AuthError`, it re-reads
the slot under the guard; if a sibling process just rotated+persisted a now-fresh
token, it skips the flip.

The lock file is created `0600` under the already-`0700` store dir; `flock` is
advisory and auto-released when the fd closes or the process dies (no stale-lock
cleanup needed).

### Thread 2 — proactive idle-token refresh (`SessionOrchestrator`)

Add a small monitor task, started in `start()`, decoupled from the usage poller's
backoff:

- `_refresh_idle_tokens_once()` iterates `list_accounts()` and, for each account
  that is **not** `needs-reauth` (a dead token can't be helped — don't hammer it)
  and has **no live session** (the SDK owns the token family for live ones),
  calls the existing `ensure_valid_token(name)` off-thread. `AuthError` /
  `TransientError` are swallowed — status transitions remain owned by the
  `usage_for` path; this task only keeps healthy tokens fresh.
- `_token_refresh_monitor()` runs it once immediately (startup recovery), then
  every `poll_interval` seconds.

This reuses `ensure_valid_token` as the single source of truth (no parallel
refresh logic) and guarantees idle tokens are refreshed even when the usage
endpoint is throttled or the daemon just restarted.

## Components & interfaces

- `ccswitch.accounts._refresh_guard(name) -> ContextManager` — nested
  thread+process lock; the only new public-ish surface.
- `SessionOrchestrator._refresh_idle_tokens_once()` / `_token_refresh_monitor()`
  — private; lifecycle-managed like `_rotation_monitor`.

## Testing

- **Thread 1:** `_refresh_guard` is held cross-fd (a second fd's
  `flock(LOCK_EX|LOCK_NB)` raises `BlockingIOError` while the guard is held —
  proving cross-process exclusion); two concurrent `ensure_valid_token` callers
  on an expired token trigger exactly **one** `refresh_access_token` (the loser
  re-reads the rotated token); `sync_back` still holds the guard during the slot
  write.
- **Thread 2:** `_refresh_idle_tokens_once` calls `ensure_valid_token` for an
  idle healthy account, **skips** a `needs-reauth` account, and **skips** an
  account with a live session; the monitor runs once on startup.

## Risks

- `flock` is POSIX/Linux-only — matches the deployment target. Documented.
- Holding the `flock` across the refresh HTTP call serialises refreshes per
  account across processes; acceptable (refresh is rare and per-account).
- No `PROTOCOL_VERSION` impact — this is all in `ccswitch.accounts` /
  orchestrator internals, not the daemon wire protocol.
