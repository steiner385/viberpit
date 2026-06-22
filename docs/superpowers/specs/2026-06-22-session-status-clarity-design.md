# Session Status Clarity — Design

**Date:** 2026-06-22
**Feature area:** 001-ccswitch-web · session status presentation
**Status:** Approved design, pending implementation plan

## Problem

A session can show **"In progress"** in the sessions list while the in-session header
top-right shows **"Ready"** — at the same moment the transcript proves the agent is mid-turn
(e.g. running bash, having just opened a PR). The two indicators disagree, and "Ready" is
simply wrong during an active turn.

Beyond the bug, the UI does not make the most operationally important distinction obvious:
**"the agent is busy doing something"** vs **"the agent is blocked, waiting for *me* to act."**
When juggling several sessions across accounts (often on mobile), the user needs to spot, at a
glance, which session needs them *now*.

### Root cause

There are **two parallel status systems** with different delivery guarantees:

1. **List status** — `derive_status()` (`server/sessions/status.py`) produces a rich vocabulary
   (`in_progress` / `waiting_input` / `ready_review` / `done` / `complete` / `needs_attention`
   / `idle`). It is **persisted** server-side (`orchestrator._persist_status` → DB + in-memory
   `SessionRecord`) and **polled** every 5s over REST (`useSessions`). Always correct on cold load.

2. **StatusBar status** — an **ephemeral** binary `working` / `idle` emitted as a WebSocket
   `status` event, reconstructed purely on the frontend reducer (`web/src/session/reducer.ts`)
   and mapped to "Working" / "Ready" by `StatusBar.tsx`.

The WS attach path (`server/bridge/ws.py`) replays the transcript and re-emits pending
permissions and questions — but **never re-emits the current live status.** So when a client
opens or reconnects to a mid-turn session, the reducer keeps its initial `{state: 'idle'}`
(`reducer.ts:59`) → "Ready" for the entire in-flight turn, while the polled list correctly
shows "In progress." That is exactly the observed inconsistency.

This is a single-source-of-truth violation (Constitution Principle VI): the same concept
("what is this session doing?") has two independent implementations that drift apart.

## Goals

- Eliminate the divergence at its root — one status vocabulary, one mapping, end to end.
- Make three states visually unmistakable everywhere a status is shown:
  - **BUSY** — the agent (or a sub-agent) is actively working.
  - **NEEDS YOU NOW** — blocked: a permission, an `AskUserQuestion`, or a plan awaiting
    approval. Nothing proceeds until the user acts. Loud.
  - **YOUR TURN** — the turn finished / session idle; the user may send the next message, but
    nothing is blocked. Calm.
  - (plus **ERROR** — failed turn, usage-limited, or ended. Loud, distinct color.)
- Surface "NEEDS YOU NOW" beyond the focused session so it finds the user across sessions.

## Non-goals (YAGNI)

- Distinguishing *main-agent* busy from *sub-agent* busy. The backend has no clean signal, and
  `in_progress` + the live elapsed readout already conveys "something is happening."
- A global header "N sessions need you" count. (Considered and dropped; the list pill + tab
  title + sort cover the multi-session need.)
- Sound / push notifications.

## Status model (shared vocabulary)

The backend continues to emit the **rich** derived status it already computes. The frontend
gains a single pure helper and presentation map used by **both** the StatusBar and the list pill.

| Tier         | Rich statuses                          | Treatment                                              | Label(s)                                                          |
|--------------|----------------------------------------|--------------------------------------------------------|-------------------------------------------------------------------|
| **busy**     | `in_progress`                          | neutral; animated `WorkingIndicator` (spinner+phrase+elapsed) | "Working…"                                                  |
| **needs-you**| `waiting_input`, `ready_review`        | **loud amber, pulsing** in list                        | sub-type specific: `ready_review`→"Approve plan"; permission→"Permission needed"; question→"Answer needed" |
| **your-turn**| `done`, `complete`, `idle`             | **calm/quiet** — static green dot, no animation        | "Your turn" (`idle` never-ran → "Ready")                          |
| **error**    | `needs_attention`, `limited`, `ended`  | **loud red**                                           | "Turn failed" / "Usage limit" / "Ended"                           |

Notes on label sub-typing for `needs-you`: the rich status alone distinguishes `ready_review`
(plan) from `waiting_input`. To split `waiting_input` into "Permission needed" vs "Answer
needed", the StatusBar can use the already-present pending permission/question events in the
reducer; the **list pill** (which only has the rich status from REST) shows "Permission needed"
as the default `waiting_input` label and "Approve plan" for `ready_review`. (A future backend
refinement could split `waiting_input` into `waiting_permission` / `waiting_question` if the
distinction proves valuable in the list; out of scope here.)

## Architecture / changes

### Backend (`server/`)

- **`sessions/driver.py`** — collapse the live status onto the rich vocabulary. `_put_status`
  currently emits `ev.Status(state="working"|"idle", count)` *and* calls
  `_emit_dashboard_status()`. Change the live `ev.Status` to carry the **rich** derived status
  string (the same value `derive_status(self._dash_signals())` yields), so the WS `status` event
  and the dashboard status are one value. `count` (queue depth) is preserved for the busy label.
- **`bridge/ws.py`** — in the attach sequence, after `replayComplete` and the pending
  permission/question re-emit, **emit the session's current live status** as a `status` frame so
  a client opening or reconnecting to a mid-turn session immediately renders the true tier. This
  single addition fixes the stale-"Ready" regression. Source the value from the driver's current
  derived status (add a small accessor, e.g. `driver.current_status()` returning
  `derive_status(self._dash_signals())`, or have the orchestrator expose the persisted record
  status). Best-effort: a failure here must not break the attach.
- The persisted dashboard-status path (`_persist_status`, REST `GET /api/sessions`) is unchanged
  — cold loads stay correct.

### Frontend (`web/`)

- **New `web/src/session/statusTier.ts`** — the single source of truth:
  - `export type StatusTier = 'busy' | 'needs-you' | 'your-turn' | 'error'`
  - `export function statusTier(state: string): StatusTier`
  - `export const STATUS_PRESENTATION: Record<...>` with `{ tier, label, icon, className }`.
- **`components/UsageBadge.tsx`** — delete the divergent `SESSION_STATUS_MAP`; re-point the
  `StatusPill` to `STATUS_PRESENTATION`.
- **`components/session/StatusBar.tsx`** — delete the local `statusText()` switch; render by
  tier from the shared map (`WorkingIndicator` for busy; amber for needs-you; calm for your-turn;
  red for error). Preserve the existing connection-state note ("Reconnecting…/Disconnected").
- **`session/reducer.ts`** — `status.state` now carries the rich value. While `replaying` is true
  and no `status` frame has arrived, render a quiet "…" placeholder rather than defaulting to a
  state that reads as "Ready".
- **Session list (`components/SessionCard.tsx` + list container)** — pill uses
  `STATUS_PRESENTATION`; `needs-you` and `error` pills pulse (respecting
  `prefers-reduced-motion`). **Sort the list by tier**: `needs-you` + `error` first, `your-turn`
  next, `busy` last; **stable within a tier** (preserve existing order). Re-sort only when a
  session's *tier* changes, not on every 5s poll, to avoid card jitter.
- **Tab title** — a small hook over `useSessions()` sets `document.title` to
  `(N) ccswitch — needs you` when N>0 sessions are in the `needs-you` tier, else `ccswitch`.

## Data flow (after)

```
driver derives rich status ──┬─► ev.Status(rich, count) ──► WS ──► reducer.status ──► StatusBar (by tier)
                             │                                  ▲
                             │                          (replayed on attach — NEW)
                             └─► _emit_dashboard_status ──► orchestrator._persist_status ──► DB
                                                                                        │
                                              GET /api/sessions (poll 5s) ──► useSessions ──┬─► list pill (by tier)
                                                                                            ├─► list sort (by tier)
                                                                                            └─► document.title
```

Both surfaces now consume the **same** rich vocabulary through the **same** presentation map.

## Error handling / edge cases

- **WS disconnected:** StatusBar keeps the last known tier and shows the existing
  "Reconnecting…/Disconnected" note — it must not flip to a "Ready"-like state on disconnect.
- **Replaying, no status yet:** show a quiet "…" placeholder, never "Ready".
- **Sort stability:** re-sort only on tier change to prevent cards reordering under the user on
  every poll.
- **Reduced motion:** pulsing pills and the spinner fall back to static treatments (the
  `WorkingIndicator` already honors `prefers-reduced-motion`).
- **`limited` / `ended` overlays:** these orchestrator overlays are not produced by
  `derive_status`; the frontend tier map must still classify them as `error` when they arrive via
  the persisted record.

## Testing

### Backend (`pytest server/tests/`)
- `status.py` `derive_status` table tests already exist — keep/extend.
- **New `ws.py` attach test (regression for the bug):** attaching to a session whose driver is
  mid-turn emits a live `status` frame carrying the in-progress status. This is the guard that
  the stale-"Ready" bug stays dead.

### Frontend (`vitest run`, `tsc --noEmit`, `eslint` in `web/`)
- `statusTier` unit table: every rich status → expected tier and label.
- `StatusBar` renders the correct tier treatment for each status (busy→WorkingIndicator,
  needs-you→amber+label, your-turn→calm, error→red); disconnect note preserved.
- List sort: orders tiers needs-you/error → your-turn → busy; stable within tier; does not
  reorder when only non-tier fields change.
- Tab-title hook: sets `(N) ccswitch — needs you` when blocking sessions exist; resets to
  `ccswitch` when none.

## Rollout

Single feature branch off `master`. Implementable in two reviewable commits if desired:
(1) backend unification + attach replay + StatusBar/shared tier map (fixes the bug, delivers the
three tiers in both surfaces); (2) reach features (loud/pulsing pill, list sort, tab title).
Both are independently shippable, but ship together to fully satisfy the request.
