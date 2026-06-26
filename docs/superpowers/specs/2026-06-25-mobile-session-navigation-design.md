# Mobile session-to-session navigation

**Date:** 2026-06-25
**Status:** Approved (design)
**Feature area:** web frontend — mobile full-screen session view

## Problem

On mobile (`< 1024px`), a single session is a full-screen route
(`/session/:sessionId` → `SessionView`). The only way out is the `← Back`
button / `Esc`, which routes back to `/sessions`. To move from one session to
another, the user must return to the list and tap a different card. There is no
"next session" concept on the full-screen route. We want to let the user move
directly from one session to the next without round-tripping through the list.

## Goals

- From a full-screen session on mobile, jump directly to the next / previous
  session with one tap.
- "Next" follows the **same order the dashboard list is currently sorted /
  grouped in**, so there are no surprises about where the user lands.
- Navigation **wraps around**: `next` on the last session goes to the first, and
  `prev` on the first goes to the last.

## Non-goals (v1)

- Swipe gestures (explicitly deferred; would collide with the existing
  left-edge "back to list" gesture). On-screen arrows only for v1.
- Respecting the dashboard's transient search filter on the full-screen route
  (see Known limitations).
- Any change to desktop (`DesktopShell`) navigation.

## Design choices (decided during brainstorming)

| Decision | Choice |
| --- | --- |
| What "next" means | The next session in the **current list order** (reuses the existing ordering engine). |
| Trigger | **On-screen chevron arrows** in the top bar (not swipe). |
| Behavior at list ends | **Wrap around** (never a dead button). |

## Architecture

### New hook: `useSessionNeighbors(sessionId)`

Location: `web/src/session/useSessionNeighbors.ts`

Owns all "what is next / previous" logic so `SessionView` stays a thin consumer
and the logic is unit-testable in isolation.

Behavior:

- Calls `useSessions()` + `useSessionOrdering(sessions)` to obtain the same flat
  `orderedSessions` array the dashboard renders (same persisted group/sort prefs,
  namespace `live`).
- Finds the current session's index in `orderedSessions`.
- Returns `{ prevId, nextId, index, total }` where `prevId` / `nextId` are
  computed with **wraparound**:
  - `nextId = orderedSessions[(index + 1) % total].id`
  - `prevId = orderedSessions[(index - 1 + total) % total].id`
- Returns **null neighbors** (`prevId === null && nextId === null`) when:
  - `total <= 1` (single session — wrapping to itself is a no-op), or
  - the current `sessionId` is not present in `orderedSessions` (archived, or a
    freshly-created `pending` session not yet returned by `/sessions`).

### UI: chevrons in `SessionView` top bar

Location: `web/src/components/session/SessionView.tsx`

Add two chevron buttons flanking the title:

```
[← Back]   ‹   <session title>   ›
```

- Reuse the existing 44px touch-target button styling already present in the top
  bar.
- `›` → `navigate('/session/${nextId}')`; `‹` → `navigate('/session/${prevId}')`.
- With wraparound the buttons are effectively always active; they are **hidden**
  only when `useSessionNeighbors` reports no neighbors (single / unknown
  session).

### Re-attach on session change

Tapping an arrow changes the `:sessionId` route param. `SessionView` must detach
the old session's websocket and attach the new one. The low-risk approach is to
give `SessionView` a React `key={sessionId}` at its route in `web/src/App.tsx`,
forcing a remount on param change. The remount runs the existing
detach-cleanup + fresh-attach effects exactly as a Back-then-open would — no
change to the websocket logic itself.

## Data flow

1. User is on `/session/A`. `SessionView` mounts, attaches to A.
2. `useSessionNeighbors('A')` computes `nextId = B` from `orderedSessions`.
3. User taps `›` → `navigate('/session/B')`.
4. Route param changes; `key={sessionId}` remounts `SessionView`, which detaches
   A and attaches B.
5. `useSessionNeighbors('B')` recomputes neighbors for the new position.

## Error handling / edge cases

- **Single session** (`total <= 1`): no neighbors → chevrons hidden.
- **Current session not in the ordered list** (archived / freshly `pending`):
  null neighbors → chevrons hidden. Back still works.
- **List re-orders while in a session** (the list polls every ~5s; triage can
  re-sort): neighbors are recomputed from the latest order, so "next" always
  reflects the current list. Expected and acceptable.
- **Pending `initialPrompt`**: arrow navigation never sets `location.state`, so
  no stale prompt is replayed into the next session.

## Known limitations (v1)

- Search-filter state lives in the Dashboard and is not carried onto the
  full-screen route, so next/prev walks the **full** ordered list, not the
  filtered subset. Acceptable for v1.

## Testing (TDD)

- **`useSessionNeighbors` unit tests** (pure logic, fast):
  - middle session → correct prev / next
  - **first wraps to last**, **last wraps to first**
  - single session → null neighbors
  - unknown id → null neighbors
- **`SessionView` component test** (jsdom, mobile branch):
  - chevrons render with 44px touch targets
  - tapping `›` calls `navigate` with the next id
  - chevrons hidden when only one session exists
- Gates: `vitest run`, `tsc -b`, `eslint` (per project norms — `tsc --noEmit` is
  a false-green here).
