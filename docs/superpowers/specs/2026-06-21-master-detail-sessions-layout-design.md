# Master-Detail Sessions Layout (Desktop) — Design

**Date:** 2026-06-21
**Status:** Approved (design); pending implementation plan
**Feature area:** 001-ccswitch-web (web frontend)

## Problem

Today the web UI is a mutually-exclusive *view state machine*. `App.tsx` holds
`view: 'dashboard' | 'session' | 'review'`, and each renders full-screen. To move
between the session list and a running session you replace the whole screen. There
is no way to see the list of sessions and a live session at the same time.

We want a **master-detail layout on desktop**: the session list lives in a left rail
(the picker/manager), the focused session fills the main area, and a single prompt
spans the bottom of the page — for both starting new sessions and messaging the
focused one.

## Goals

- Wide screens show the session list and the focused session simultaneously.
- One unified bottom prompt: creates a new session *or* messages the focused one,
  driven entirely by what is selected in the list.
- Keep the existing mobile/touch experience working, untouched.
- Reuse the existing single-socket session architecture without rearchitecting it.

## Non-goals

- No responsive master-detail on phones (explicitly desktop-only for v1; mobile keeps
  the current full-screen navigation).
- No keeping multiple sessions "live" in the background. One focused session is
  mounted/attached at a time.
- No changes to `ws/client.ts` or the server protocol.

## Decisions (from brainstorming)

1. **Desktop-only redesign.** Side-by-side layout only at the `lg` breakpoint
   (≥1024px). Below `lg`, the existing `view` state machine renders exactly as today.
2. **One unified, context-aware bottom prompt.** Replaces the split between the
   `DispatchBar` (new session) and the per-session `Composer` (message session).
3. **Selection is the single source of truth.** A permanent "✦ New Session" slot
   sits in the sidebar list. Selecting it targets *creation*; selecting a real
   session targets *that session*. No hidden modes.
4. **Tabs atop the sidebar.** Sessions / Accounts / Archived remain tabs, positioned
   in the left rail.
5. **Accounts/Archived take over the main pane.** When those tabs are active, their
   (wider, unchanged) content renders full-width in the main pane. The sidebar still
   lists sessions so the user can switch back. `AccountRow` is not restyled.
6. **Default selection on load: the New Session slot.** Main pane shows a welcome /
   empty state; the bottom prompt is ready to create.
7. **Single focused session mounted at a time.** Switching unmounts the old
   `SessionView` (detaches) and mounts the new (attaches → server replays history).

## Layout

```
┌───────────────────────────────────────────────┐
│  header: ccswitch                              │
├──────────────┬────────────────────────────────┤
│  SIDEBAR     │  MAIN PANE                      │
│  [tabs]      │                                 │
│  Sessions/   │  focused session (transcript +  │
│  Accounts/   │  status)  — OR full-width       │
│  Archived    │  Accounts / Archived content    │
│              │                                 │
│  ✦ New Session                                 │
│  • session a │                                 │
│  • session b │                                 │
├──────────────┴────────────────────────────────┤
│  UNIFIED BOTTOM PROMPT (spans full width)      │
└───────────────────────────────────────────────┘
```

- Sidebar width: ~280–320px (fixed), main pane flexes.
- The header, sidebar, and bottom prompt are fixed; only the main pane and the
  sidebar list scroll independently.

## Components & responsibilities

### `App.tsx` — layout owner + selection state
- Renders the wide-screen master-detail layout at `lg+`; renders the existing
  `view`-state-machine layout below `lg`.
- Owns `selectedTarget: { type: 'new' } | { type: 'session'; id: string }`,
  initialized to `{ type: 'new' }`.
- Owns the active sidebar `tab: 'sessions' | 'accounts' | 'archived'`.
- The main pane chooses what to render from `tab` + `selectedTarget`:
  - `tab === 'sessions'` → `SessionView` keyed by `selectedTarget.id`, or a welcome
    empty state when `selectedTarget.type === 'new'`.
  - `tab === 'accounts'` → full-width accounts manager.
  - `tab === 'archived'` → full-width archived list; "Review" renders the read-only
    transcript in the main pane (no separate full-screen route on desktop).

> The mobile (`< lg`) branch keeps the current `view`/`handleOpenSession`/
> `handleReviewSession`/`handleBack` flow verbatim. The new state lives alongside it.

### Sidebar (`SessionSidebar`, new)
- Hosts the tab bar (extracted/shared with the existing Dashboard tabs).
- Renders the session list with a permanent "✦ New Session" row at the top.
- Selecting a row updates `selectedTarget`. The selected row is visually highlighted.
- Reuses the existing keyboard navigation semantics (see below).

### Unified bottom prompt (`SessionPrompt`, new — consolidates `DispatchBar` + `Composer`)
- Reads `selectedTarget` to decide behavior and accessory chrome:
  - **target `new`**: repo selector + default permission mode; Enter creates a session
    via `useCreateSession`, then auto-selects the created session
    (`selectedTarget = { type: 'session', id }`).
  - **target `session`**: sends `prompt(id, text)`; shows interrupt button,
    permission-mode cycle (Shift+Tab), queued-message list + cancel, and the
    working/idle status — all scoped to the focused session.
- Same text box across both modes; only the surrounding controls swap.

### `SessionView` — unchanged internals
- Still owns its reducer and the attach/detach lifecycle. In the new layout it is
  mounted in the main pane and **keyed by `sessionId`** so switching selection
  remounts it (clean detach/attach + replay). Its own top "← Back" bar and embedded
  `Composer` are hidden/omitted in the wide-screen layout (the unified bottom prompt
  replaces the composer; selection replaces Back). On mobile it renders as today.

## Keyboard model

The existing `DispatchBar` keyboard behavior moves into the unified prompt and now
drives the sidebar selection:
- `↑`/`↓` at a line boundary (or empty prompt) → move selection through the sidebar
  list, including the "New Session" slot.
- `Enter` with text → create (target `new`) or send (target `session`).
- `Enter`/`→` on empty prompt → focus the selected session in the main pane.
- `r`/`F2`, `Delete` (twice), `Alt+1..9` on empty prompt → row actions against the
  selected session, as today.
- `Esc` clears the draft / clears selection back to `new`.
- `Shift+Tab` (target `session`) → cycle the focused session's permission mode.

## WebSocket lifecycle

No protocol or client changes. The socket singleton tracks one `_activeSessionId`;
mounting exactly one `SessionView` keeps that invariant. Switching selection unmounts
the previous view (its cleanup calls `detach`) and mounts the next (its effect calls
`attach`), and the server replays history on attach.

**Tradeoff:** switching sessions replays history instead of being instant. Accepted
for v1. A future enhancement could keep background sessions live, but that requires
multi-attach support, reconnect re-attaching a *set*, and per-`sessionId` filtering of
the `sessionEvent` fan-out (today `SessionView` dispatches every event into its
reducer, safe only because one is mounted).

## Testing

- **Layout branch:** at `lg+`, sidebar + main pane + bottom prompt all render together;
  below `lg`, the existing view-state-machine markup renders (assert the new grid is
  absent). Use a width/matchMedia stub.
- **Selection → prompt mode:** selecting "New Session" shows the repo selector and
  creates on Enter; selecting a real session shows composer chrome and sends on Enter.
- **Auto-select after create:** creating from the New slot selects the new session.
- **Tab → main pane:** Accounts/Archived tabs render their content in the main pane;
  sidebar still lists sessions; Review renders the transcript in the main pane.
- **Keyboard:** `↑/↓` moves selection including the New slot; row actions still fire on
  empty prompt; existing `DispatchBar`/`KeyboardSessions` tests are migrated/extended.
- **Single-mount invariant:** switching selection unmounts the prior `SessionView`
  (detach called) and mounts the new (attach called) — assert via the socket spy.
- Reuse/extend existing suites: `DashboardTabs`, `KeyboardSessions`, `DispatchBar`,
  `SessionList`, `ArchivedTab`, `a11y`.

## Risks / mitigations

- **Regressing mobile.** Mitigated by making wide-screen an additive branch that
  leaves the `< lg` path byte-for-byte intact.
- **Two prompts merged into one** is the highest-risk refactor. Mitigation: build
  `SessionPrompt` to delegate to the existing create/prompt hooks and port the
  existing keyboard tests before refactoring callers.
- **Replay-on-switch feels heavy.** Accepted for v1; documented upgrade path above.
