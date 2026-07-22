# Max Active Sessions + Server-Side Priority Queue — Design

**Date:** 2026-07-21
**Status:** approved by Tony (global cap · automations queue too · sidebar queue UI)

## Goal

A configurable global cap on concurrently *active* sessions (default **5**). Any session
created beyond the cap — by the UI or by any automation — is queued server-side instead of
spawned, drains automatically in priority order as slots free, and can be reprioritized
from a queue UI in the sessions sidebar.

Motivation: the 2026-07-21 incident showed total session load (each session = claude +
Xvfb + MCP fleet inside the daemon cgroup) can pin the daemon at its MemoryHigh throttle.
Per-automation limits don't bound the *total*; this cap does.

## Decisions (user-confirmed)

1. **Global cap** — counts every session regardless of origin (UI, backlog-runner,
   pr-doctor, autopilot). Per-automation limits still apply beneath it.
2. **Automations queue too** — at cap, an automation's spawn enqueues exactly like a user
   spawn (no rejection path). Accepted trade-off: an automation's claim/branch context may
   age while queued.
3. **Queue UI lives in the sessions sidebar** — a collapsible "Queued (N)" section above
   the session list. No new dashboard tab (avoids the TAB_DEFS fan-out to ~15 test files).

## Architecture

Queued sessions are **real Session rows** with a new `"queued"` status, plus a
`session_queue` table holding ordering and the deferred spawn parameters. Rejected
alternatives: a pending-requests table with no Session row (automations get no session id
back; duplicates session display machinery) and per-origin caps without a queue (doesn't
meet the requirement).

### Status semantics (single source of truth)

- New status literal: `"queued"`.
- `INACTIVE_SESSION_STATUSES` stays `{"ended", "archived"}` (a queued session is neither
  reviewable nor dead).
- New SoT helper in `server/db/models.py`, beside `INACTIVE_SESSION_STATUSES`:
  `NON_ACTIVE_SESSION_STATUSES = INACTIVE_SESSION_STATUSES | {"queued"}` — "active" (slot-
  consuming) means `status not in NON_ACTIVE_SESSION_STATUSES`. Every cap/slot count uses
  this helper; no inline status math anywhere else.
- Sweep existing liveness checks for `"queued"` handling: session sweeps must not treat
  queued sessions as stuck/attention-worthy; spawn dedup must treat a queued session for a
  target as live (it already will, since queued ≠ ended/archived).

### Data model

New table `session_queue` (created by `create_all`; no ALTER needed):

| column | type | notes |
|---|---|---|
| `session_id` | String PK, FK sessions.id | one queue row per queued session |
| `priority` | Integer, not null | lower = sooner; reorders rewrite affected rows in one transaction (queue is small) |
| `enqueued_at` | tz-aware DateTime, not null | FIFO tiebreak within equal priority |
| `params` | JSON, not null | deferred spawn kwargs: initial_prompt, cols, rows, branch, env overrides, model, permission mode — everything `create_session` needs that the Session row doesn't hold |

`AppSettings` gains key `maxActiveSessions` (int, default **5**, min 1). No Session-table
schema change.

### Orchestrator flow

- **Enqueue:** `create_session` (the single spawn choke point) counts active sessions;
  at/over the cap it writes the Session row with `status="queued"` + a `session_queue` row
  (priority = current max priority + 1, i.e. back of queue), emits a `session_queued`
  event, and returns the record. No worktree, no account rotation, no driver.
- **Deferred decisions at dequeue:** worktree creation and account selection (when the
  caller didn't pin one) happen when the session *starts*, not when it enqueues — no stale
  checkouts, no disk held while waiting, usage-aware rotation stays current. (Automation
  spawns pin their account at enqueue time; if it's unhealthy by dequeue, the start fails
  into the error path below — accepted v1 limitation.)
- **Drain:** `_maybe_start_queued()` — while `active_count < max` and queue non-empty,
  pop by `(priority, enqueued_at)` and run the normal spawn pipeline. Triggered by: any
  session leaving the active set (close, archive, driver exit), the cap being raised, and
  daemon start (queued rows survive restarts; rehydrate calls the drain after records
  rebuild).
- **Start failure** (e.g. `DiskSpaceError`, dead account): remove the queue row, set the
  session `needs_attention`, emit `session_queue_start_failed`, continue to the next
  entry. Existing stuck-session machinery and the user handle retry.
- **Cancel:** removing a queued entry deletes the queue row and marks the session
  `ended` (event `session_queue_cancelled`).
- **Lowering the cap** never kills running sessions — it only gates new starts.

### API + daemon plumbing

- `POST /api/sessions` unchanged in shape; the response may carry `status: "queued"`.
- `GET /api/sessions` already returns all non-archived sessions; queued ones ride along
  with their status plus `queuePosition` (computed) in the payload.
- New: `POST /api/sessions/{id}/queue-move` body `{"op": "up" | "down" | "top" | "position", "position"?: int}`;
  `DELETE /api/sessions/{id}/queue` (cancel).
- Every new orchestrator method the API calls gets a `RemoteOrchestrator` proxy + a
  registered RPC (breadth tests enforce this).
- Freshness: queued sessions ride the existing `GET /sessions` payload (5s poll); the
  mutating client invalidates the sessions query immediately after a move/cancel, other
  clients converge on the next poll. No per-session WS frames for queued sessions — they
  have no driver and no WS connection (v1 simplification).
- Settings: `maxActiveSessions` PATCHable through the existing app-settings endpoint; the
  daemon re-reads it and drains on increase.

### UI

- **"Queued (N)"** collapsible section above the session list in the sidebar. Rows show
  name, repo, position badge; controls (move-up, move-down, move-to-top, remove) live in
  the card's **actions group**, never inside the row's open-button (nested-interactive
  a11y). Buttons, not drag-and-drop — mobile-friendly.
- Opening a queued session routes to the normal session view showing a
  "Queued — position N of M" state (no terminal yet).
- Settings surface gets a numeric "Max active sessions" field (min 1).
- Known cost: any new hook SessionList consumes breaks ~16 `@/api/hooks` `vi.mock` lists —
  budget the sweep.

## Non-goals (v1)

- No per-repo or per-account sub-caps.
- No preemption: lowering the cap or reprioritizing never stops a running session.
- Unarchive/resume of existing sessions bypasses the cap (only `create_session` gates).
- No queue ETA estimates; no drag-and-drop reordering.

## Testing

- **Orchestrator:** cap boundary (5th spawns, 6th queues), drain order incl. priority
  ties (FIFO within priority), reorder ops, cancel, restart rehydrate of queued rows,
  dequeue-time account/worktree deferral, start-failure path, automations' spawns queue
  (not error) at cap, cap raise drains / lower doesn't kill.
- **API:** queue-move ops + validation, cancel, settings PATCH bounds (min 1), RPC proxy
  breadth.
- **Web (vitest):** queue section renders/sorts, controls call the right endpoints,
  position badge, settings field; jsdom offset stub notes apply to any virtualized list.
- **DB:** `session_queue` created on fresh dev DB via `create_all`; queued rows visible
  after simulated restart.
