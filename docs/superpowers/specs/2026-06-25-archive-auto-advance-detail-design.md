# Auto-advance the detail view when the open session is archived

**Date:** 2026-06-25
**Status:** Design — awaiting review
**Scope:** Frontend only (`web/src`). No backend / API changes.

## Problem

When you archive the session you're currently viewing, the detail view drops you
back to the list (mobile) or the empty "New Session" welcome pane (desktop). For
someone working through a queue of sessions, that's a dead-end every time: you
archive, land on nothing, then have to re-find your place in the list. The detail
view should instead **advance to the next session**, so archiving feels like
clearing the top card off a stack.

## Desired behavior

When the **currently-viewed** session leaves the live list, switch the detail view
to the session that **takes its place** in the current ordered view:

- The session directly **below** it in the list.
- If it was the **last** row, the session **above** it.
- If it was the **only** row (list now empty), fall back: mobile → sessions list,
  desktop → welcome pane (today's behavior).

"The list" means the **current filtered + sorted view** the user is looking at:
the live `useSessions()` data, ordered/grouped by the persisted `live` ordering
prefs, with collapsed groups' rows excluded, and (desktop only) the active search
filter applied. Archived sessions are never a target — only non-hidden (live) ones.

This applies only when the session being removed **is the one open in the detail
view**. Archiving some *other* card from the desktop sidebar leaves the detail
pane untouched (current `handleClose` already guards on this).

### Which triggers advance

The unifying rule is "the open session just disappeared from the list." That
covers every path that removes the open session:

| Trigger | Location | Today | New |
|---|---|---|---|
| Archive button (detail header) | `SessionActions` via `onDone` | → list / welcome | → neighbor |
| Delete (detail header, archive-then-delete) | `SessionActions` via `onDone` | → list / welcome | → neighbor |
| Archive card (desktop sidebar) | `DesktopShell.handleClose` | → welcome (if it was selected) | → neighbor |
| Permanent-delete card (desktop sidebar) | `DesktopShell.confirmSessionDelete` | → welcome (if it was selected) | → neighbor |

Back button / `Esc` / `←` detach gestures are **unchanged** — they still go to the
list / welcome. Only the removal paths advance.

## Architecture

### 1. The selection logic — one pure, tested helper

New module `web/src/session/nextAfterArchive.ts`:

```ts
/**
 * Given the ids of the currently-visible session list (in display order) and the
 * id being removed, return the session the detail view should advance to:
 *   - the row directly below it, else
 *   - the row directly above it (it was last), else
 *   - null (it was the only row, or isn't in the visible list → caller falls back).
 */
export function nextVisibleSessionId(
  orderedIds: readonly string[],
  removedId: string,
): string | null
```

All the edge-case reasoning lives here and is unit-tested in isolation
(`nextAfterArchive.test.ts`): middle row, last row, single row, id-not-present.
A `removedId` not in `orderedIds` (e.g. the open session is inside a collapsed
group, so it isn't in the visible flattened list) returns `null` → caller falls
back to list/welcome, which is the honest "no visible neighbor" outcome.

### 2. The timing contract — capture the pre-archive list

`useCloseSession.onMutate` removes the row from the `QK.sessions` cache
synchronously, before the mutation resolves. So "next" must be computed against
the **pre-archive** ordered list.

We get this for free by computing inside the **click-time closure**: a React event
handler reads the state from the render in which it was created, and that render's
`orderedSessions` predates the optimistic removal. Concretely:

- `SessionActions.handleArchive` already does
  `closeSession.mutate(id, { onSuccess: onDone })`. The `onDone` it captures is the
  host's closure from the click-time render. **No change to `SessionActions`** — we
  only change what the host passes as `onDone`.
- `DesktopShell.handleClose` / `confirmSessionDelete` run synchronously on click,
  so `orderedSessions` read inside them is pre-archive.

Switching happens on `onSuccess` (preserving today's "commit on server confirm,
revert + toast on error" semantics), but the *target id* is computed from the
captured pre-archive list, so the timing of the success callback doesn't matter.

### 3. Wiring — desktop (`DesktopShell.tsx`)

`DesktopShell` already holds the visible ordered list as `orderedSessions` (filtered
by `sessionSearch.matchedIds`, sorted, collapse-flattened). Add one helper that maps
a removed id to the next selection target:

```ts
const advanceFrom = useCallback((removedId: string): Target => {
  const next = nextVisibleSessionId(orderedSessions.map((s) => s.id), removedId)
  return next ? { type: 'session', id: next } : { type: 'new' }
}, [orderedSessions])
```

Apply it at the three desktop sites where the open session leaves:

- **Detail-header archive/delete:** the `onClosed` passed to `MainPaneSession`
  (currently `() => setSelection({ type: 'new' })`) becomes
  `() => setSelection(advanceFrom(selection.id))`.
- **Sidebar card archive (`handleClose`):** when `cur.id === id`,
  `setSelection(advanceFrom(id))` instead of `{ type: 'new' }`.
- **Sidebar permanent-delete (`confirmSessionDelete`):** same substitution in its
  `onSuccess`.

`SessionRuntimeProvider` is keyed by `selection.id`, so changing the selection to the
neighbor tears down the old session (detach) and attaches the new one automatically.

### 4. Wiring — mobile (`SessionView.tsx` + `SessionRoute` in `App.tsx`)

`SessionView` currently feeds the same `handleBack` to both the Back button and
`SessionActions.onDone`. Split them:

- Keep `onBack` for the Back button / `Esc` / `←` → navigate to `/sessions`
  (unchanged).
- Add a distinct `onDone` prop on `SessionView`, wired into `SessionActions.onDone`.
  It detaches the current session and navigates to the neighbor or the list.

`SessionRoute` (the only place that knows the URL/router) computes the neighbor.
It currently has no list; give it one:

```tsx
function SessionRoute() {
  const { sessionId } = useParams()
  const navigate = useNavigate()
  const { data: sessions = [] } = useSessions()
  const { orderedSessions } = useSessionOrdering(sessions) // persisted order, no search filter
  // ...
  const handleArchived = () => {
    const next = nextVisibleSessionId(orderedSessions.map((s) => s.id), sessionId!)
    navigate(next ? `/session/${next}` : '/sessions')
  }
  return <SessionView sessionId={sessionId} onBack={() => navigate('/sessions')}
                      onDone={handleArchived} initialPrompt={initialPrompt} />
}
```

`useSessionOrdering(sessions)` here reconstructs the same persisted sort/group/collapse
order the Dashboard showed. No search filter is applied on mobile because the search
box isn't reachable from inside a full-screen session — the honest "current view" the
user returns to is the unfiltered ordered list. Navigating to `/session/{next}` swaps
the route param, remounting `SessionView` on the neighbor (fresh runtime).

## Component boundaries

- `nextAfterArchive.ts` — pure selection logic. Input: ordered ids + removed id.
  Output: next id or null. No React, no data fetching. Independently testable.
- `SessionActions.tsx` — **unchanged.** Still fires a single `onDone` for archive and
  delete; doesn't know about ordering.
- `DesktopShell.tsx` — owns desktop selection state; gains one `advanceFrom` helper
  and three one-line call-site swaps.
- `SessionView.tsx` — gains an `onDone` prop distinct from `onBack`; the body and
  gestures are otherwise unchanged.
- `App.tsx` `SessionRoute` — gains the ordered-list read and the archived → neighbor
  navigation.

## Testing

**Unit (`nextAfterArchive.test.ts`):**
- middle row → returns the row below
- last row → returns the row above
- single row → returns `null`
- id not present → returns `null`

**Component:**
- Desktop: archiving the open session (detail header) advances `selection` to the
  neighbor; archiving the last/only session falls back to the welcome pane; archiving
  a *non-selected* sidebar card leaves the detail pane on its current session.
- Mobile: archiving the open session navigates to `/session/{neighbor}`; archiving the
  only session navigates to `/sessions`; the Back button still navigates to `/sessions`
  (no advance).

**Unchanged:** `useCloseSession` optimistic removal, Undo toast, and error-revert
behavior are not touched.

## Out of scope / non-goals

- No backend or API changes.
- No change to the Undo toast or optimistic-removal mechanics.
- No new ordering, filter, or "next" preference surface — we reuse the existing
  persisted `live` ordering and the desktop search filter as-is.
- Bulk-archive flows (archived-manager multi-select) are not in scope; this is about
  archiving the single session you're viewing.
