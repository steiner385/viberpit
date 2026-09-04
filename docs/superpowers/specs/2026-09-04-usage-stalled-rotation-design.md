# Usage-stalled sessions: rotate-to-best shortcut, visible pause reasons, and recovery gaps

**Date:** 2026-09-04
**Status:** approved (design), PR 1 of 2
**Follow-up:** PR 2 — model-aware account selection (per-model `weekly_scoped` limits)

## Problem

Sessions whose account has spent its usage stop making progress and show up as red
"Turn failed" / "Usage limit" cards. The user has to notice, open each one, pick another
account by hand, and re-send the prompt. The request: *switch to the next best account and
continue the conversation* — automatically where possible, and with a one-click shortcut
where a human is in the loop.

## Evidence (rocky daemon journal, 2026-09-04)

Between 10:21 and 10:38 EDT five sessions on an account at its 5-hour ceiling were paused
with `all_accounts_busy` roughly once a minute (the poller's driverless-restart path), then
hit the per-session rotation cap (`rotation_cap`) and parked as `needs_attention`. They sat
as "Turn failed" until the 10:44 daemon restart, after which the user clicked "Resume
interrupted". Account state at the time of writing: two usable, two at the 5-hour ceiling,
one at the weekly ceiling (`session_pct` 0, `weekly_pct` 100), one disabled.

## What already exists

- `_rotation_monitor` (poller-driven) rotates un-pinned sessions when an account reaches
  `_ROTATION_CEILING` on `session_pct`; pinned sessions are set to `limited`.
- `_recover_session` handles a 429 mid-turn: quarantine, `_rotate_away`, replay the last
  prompt. `AllAccountsBusy` → `_pause_session(all_accounts_busy)` → `needs_attention`.
- `_stuck_session_sweep` retries `_rotate_away` for every owned `needs_attention` record
  every 300 s.
- `_reassign` is the single driver-swap primitive (bounded interrupt, close, resume on the
  new account, re-apply model, replay prompt, `rotated` event). `switch_account_manual`
  and `reload_session` both route through it.

## Gaps this PR closes

1. **The pause reason is invisible.** `_pause_session` publishes a `paused` WS event with
   `reason` and `earliestReset`; the web client ignores it. `needs_attention` renders as
   "Turn failed" whether the cause is a crash cap or "every account is at its ceiling until
   12:10". Nothing is persisted, so a poll or a reload loses it entirely.
2. **The pin trap.** `switch_account_manual` pins the session. There is no unpin control
   anywhere in the API or UI. When the pinned account hits its ceiling the session goes
   `limited` and no loop ever touches it again.
3. **Only the 5-hour percent counts as "dead".** `_rotation_monitor` and
   `_account_is_dead` ignore `weekly_pct`. The selector already drops weekly-at-ceiling
   accounts for *new* picks, so a session sitting on a weekly-exhausted account is not
   moved until its next turn 429s.
4. **Recovery is time-driven only.** Nothing reacts when an account's window resets; the
   sweep waits up to 300 s.
5. **No shortcut.** No batch action for usage-stalled sessions. The per-session picker
   forces choosing a specific account and pins it.

## Non-goals

- Per-model limit awareness in selection (`weekly_scoped` entries in the usage payload).
  That is PR 2 and depends on the `Usage` model growing a per-model map.
- Changing the sweep interval, the rotation cap, or autopilot behaviour.
- Auto-unpinning a pinned session. Decision (user, 2026-09-04): an explicit pick of a
  specific account still means "stay here"; a `limited` session resumes on its own account
  once that account's window resets; the new next-best action is the only thing that
  clears a pin, because it is an explicit "move me" request.

## Design

### 1. Persisted pause reason

**DB (`server/db/models.py`, migration `0023_session_pause_reason`):** two nullable
columns on `sessions`, picked up by `_reconcile_columns` on both dialects:

- `paused_reason: str | None` — the `_pause_session` reason token
  (`all_accounts_busy`, `rotation_cap`, `crash_cap`, `stall_cap`, `turn_dropped_cap`,
  `invalid_rotation_target`, `reassign_failed:<r>`) or `pinned_account_limited` for the
  `limited` overlay.
- `paused_until: datetime | None` — naive UTC. The earliest known reset
  (`AllAccountsBusy.earliest_reset`, or the pinned account's `session_resets_at` /
  `weekly_resets_at`, whichever is binding). NULL when unknown.

**Record (`server/sessions/types.py`):** `paused_reason`, `paused_until` fields; both in
`to_dict` / `from_dict` (`pausedReason`, `pausedUntil` ISO) so the daemon→edge RPC carries
them; `_record_from_row` maps them.

**Single writer:** `_persist_status` is already the one place a status reaches the row and
the WS. It gains:

- when `status not in {"needs_attention", "limited"}`: clear both fields in memory and in
  the same UPDATE (the session is doing something again);
- the `status` WS frame carries `pausedReason`, `pausedUntil`, `usageStalled`.

`_pause_session` sets `rec.paused_reason` / `rec.paused_until` *before* calling
`_persist_status("needs_attention")`, which writes them in the same UPDATE. The
`_rotation_monitor` pinned branch does the same with `pinned_account_limited` before its
`_persist_status("limited")`. `_reassign_locked` clears both on success alongside the
`account_name` write (a rotation may not emit a status until the replayed turn starts).

**Derived flag, one definition:** `server/sessions/usage_stall.py`:

```python
USAGE_STALL_REASONS = frozenset({"all_accounts_busy", "rotation_cap", "pinned_account_limited"})

def is_usage_stalled(status: str, paused_reason: str | None) -> bool:
    return status == "limited" or (status == "needs_attention" and paused_reason in USAGE_STALL_REASONS)
```

Emitted as `usageStalled` by both serializers (`SessionRecord.to_dict` and
`api/sessions._record_to_dict`) — the same two-serializer rule `resumable` follows.

### 2. Rotate-to-best primitive and unpin

**`SessionOrchestrator.rotate_to_best(session_id, identity=SYSTEM_IDENTITY) -> str`**
(next to `reload_session`, same contract shape):

- `skipped:not-found`; `skipped:<status>` for `_NOT_RELOADABLE_STATUSES`.
- `exclude = quarantined | {record.account}`; `select_account(exclude, load)`.
  `AllAccountsBusy` → if the session is already usage-stalled, re-run `_pause_session`
  with the fresh `earliest_reset` (refreshes `paused_until`); return
  `skipped:all_accounts_busy`. A healthy session is left untouched.
- Un-pin (`account_pinned=False`, `_persist_pin`). Do **not** call
  `_recovery.record_rotation` — the cap guards *automatic* thrash; a human asking to move
  is not thrash.
- `_reassign(record, new, reason="manual_rotate", replay_last_prompt=status in
  _REPLAY_STATUSES | {"needs_attention", "limited"})`. A stalled session's last prompt is
  the one that failed, so it is replayed; a session parked on a question / permission /
  review resumes and waits, exactly the reload rule.
- `no_driver` if `_reassign` left no driver; else `_rethread_identity(driver, identity)`,
  `log_event("session.rotate_to_best")`, return `rotated`.

**`SessionOrchestrator.set_account_pin(session_id, pinned: bool) -> str`**:

- `pinned=True`: set + persist (pins the *current* account).
- `pinned=False`: clear + persist; if `status == "limited"` also spawn
  `_rotate_away(record, reason="unpinned")` so the session moves immediately.
- Returns `pinned` / `unpinned` / `skipped:not-found`.

**Plumbing** (each is one line, mirrored from `reload_session`): `facade.py`,
`remote.py` (RPC proxy), `fleet_orchestrator._PER_SESSION`, `orchestrator_rpc.py`
allowlist.

**HTTP (`server/api/sessions.py`):**

- `POST /sessions/{id}/rotate` → 404 unknown, 409 `{code: <skip reason>}`, else the
  updated record; audit `session.rotate`.
- `POST /sessions/batch/rotate` `{ids}` → `{succeeded, failed}` like `batch/resume`;
  only `rotated` counts as success; audit per success.
- `POST /sessions/{id}/account-pin` `{pinned: bool}` → updated record; audit
  `session.account_pin`.

### 3. Automation

**a. Weekly ceiling counts as dead.** New helper in `src/ccswitch/selector.py`:
`at_ceiling(u: Usage, ceiling: float) -> bool` = `session_pct >= ceiling or weekly_pct >=
ceiling`. `rank_accounts` uses it (replacing its inline check) and so do
`_rotation_monitor` and `_account_is_dead` (with `_ROTATION_CEILING`). The monitor's reason
string becomes `session limit reached` / `weekly limit reached` by which dimension is
binding.

**b. `limited` sessions recover on reset.** `_sweep_needs_attention` also visits owned
`limited` records: if the poller's cached usage for the pinned account is now below
ceiling → `_reassign(record, record.account, reason="limit_reset")` (replays the last
prompt; the pin is honoured). Still at ceiling, or no cached usage → skip.

**c. Recovery-triggered sweep.** `_rotation_monitor` currently `continue`s when an event
is below ceiling. Before continuing, it calls `_maybe_sweep_on_recovery(now)`: if any
owned record is usage-stalled and at least `_RECOVERY_SWEEP_DEBOUNCE_S = 30` s have passed
since the last recovery-triggered sweep, spawn `_sweep_needs_attention()`. A freed account
helps every stalled session, so the sweep is not scoped to the event's account. The
periodic 300 s sweep stays as the backstop.

**d. `earliest_reset` reaches the row.** `_pause_session` already receives it; it now
lands in `paused_until` (see §1).

### 4. UI

**Types (`web/src/api/types.ts`):** `pausedReason?: string | null`,
`pausedUntil?: string | null`, `usageStalled?: boolean` on `Session`.

**Status pill (`web/src/session/statusTier.ts`):** `StatusPresentationOpts` gains
`usageStalled?` and `pausedUntil?`. After the liveness and restart checks, when
`usageStalled` and the status is `needs_attention` or `limited`, return a `waiting`-tier
presentation (Hourglass icon, `NEEDS_YOU` text class):

- `needs_attention` → `Waiting for capacity` + ` · retries <time>` when `pausedUntil`
  is present (formatted with the existing reset-time formatter used by
  `AllLimitedBanner`).
- `limited` → `Usage limit (pinned)` + ` · resets <time>`.

Callers that already thread `interruptedByRestart` (`triageTier.sessionTriage`,
`UsageBadge.StatusPill`) thread the two new opts the same way.

**WS client (`web/src/ws/client.ts`):** `WsStatusMsg` gains the three fields;
`_patchSessionStatus` applies them; `_patchSessionAccount` (the `rotated` frame) sets
`usageStalled: false, pausedReason: null, pausedUntil: null`.

**Shortcut (`web/src/components/SessionList.tsx`):** `stalledIds = sessions.filter(s =>
s.usageStalled)`; a toolbar button `Rotate stalled (N)` (Shuffle icon) beside "Resume
interrupted", backed by `useRotateStalled()` → `POST /sessions/batch/rotate` with the same
toast shape as `useResumeInterrupted`. `SessionList` is shared by both shells, so one wiring
site covers phone and desktop.

**Per-session (`web/src/components/session/SessionActions.tsx`):** `onRotate?: () => void`
→ menu item `Next best account` (Shuffle icon), wired like `onReload` through
`SessionHeader` from both `MainPaneSession` (desktop) and the mobile session view, backed by
`useRotateSession()` → `POST /sessions/{id}/rotate`. The host re-attaches the socket on
success exactly as reload does.

**Unpin (`web/src/components/session/SessionMeta.tsx`):** the 📌 span becomes a button
(`aria-label="Unpin account"`, title "Pinned — click to unpin") calling `onUnpin`, which
`SessionBody` backs with `useSetAccountPin()` → `POST /sessions/{id}/account-pin
{pinned:false}`.

### Error handling

- `rotate_to_best` never raises for a known session: every failure is a returned token
  (`skipped:*`, `no_driver`). `_reassign` already pauses with `reassign_failed:*` and
  logs on a failed rebuild.
- The batch endpoint is best-effort: one failure never aborts the loop.
- `set_account_pin(False)` on a `limited` session spawns the rotation; the HTTP response
  returns immediately with `accountPinned=false` and the row still `limited` until the
  rotation lands (the `rotated` frame patches the list).

### Testing

Server (`server/tests/`, pytest, fake orchestrators as in `test_stuck_session_sweep.py` /
`test_manual_switch_and_pin.py` / `test_dead_account_rotation.py`):

- `test_rotate_to_best.py`: not-found / not-reloadable skips; all-busy on a stalled
  session refreshes `paused_until` and returns the skip token; all-busy on a healthy
  session touches nothing; un-pins; does not record a rotation; replay decision per status;
  `no_driver`; identity re-threaded.
- `test_account_pin.py`: pin / unpin persist; unpin on `limited` spawns `_rotate_away`.
- `test_pause_reason.py`: `_pause_session` persists reason + until; `_persist_status`
  clears on non-stalled status and carries the fields in the WS frame; `_reassign_locked`
  clears on success; `is_usage_stalled` table; both serializers emit `usageStalled`.
- Extend `test_dead_account_rotation.py`: weekly ceiling rotates (monitor) and counts as
  dead (`_account_is_dead`); `at_ceiling` unit test in `tests/` (core).
- Extend `test_stuck_session_sweep.py`: `limited` + account recovered → same-account
  reassign; still at ceiling → untouched; recovery-triggered sweep fires once per debounce
  window and only when a stalled record exists.
- `test_sessions_rotate_routes.py`: `/rotate`, `/batch/rotate`, `/account-pin` contracts
  (mirrors `test_sessions_batch_resume.py`).
- Schema: `tests/test_schema_reconcile_coverage.py` needs no entry (nullable columns).

Web (`web/`, vitest): `statusTier` presentation for the two stalled shapes; `client.ts`
status/rotated patches; `SessionList` shows/hides the button and posts the stalled ids
(run the FULL vitest suite — manual `@/api/hooks` mocks in Dashboard tests break when a
shared component gains a hook); `SessionActions` renders the new item only when `onRotate`
is passed; `SessionMeta` unpin button.

Gates before push: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`,
`pytest tests/`, `pytest server/tests/`, and in `web/`: `vitest run`, `tsc -b`, `eslint`.
Core edits under `src/ccswitch/` must run with `PYTHONPATH=<checkout>/src` (the shared venv
imports the deploy worktree otherwise).
