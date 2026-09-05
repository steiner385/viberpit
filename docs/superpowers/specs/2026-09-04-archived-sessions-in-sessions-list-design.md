# Archived sessions inside the Sessions list — design

**Date:** 2026-09-04
**Status:** proposed (awaiting review)
**Scope:** `web/` (shell, sessions feature, components), one backend field fix

## 1. Problem

Archived sessions live on their own page (`/archived`, `ArchivedManager.tsx`, 581 lines) with
their own row layout, bulk bar, selection logic, delete dialog, empty states and time
formatting. The live Sessions list (`SessionListColumn` → `SessionList` → `SessionCard`) has a
second copy of each of those. The two already share the search hook, the ordering hook,
`SessionListControls` and `GroupedList`; everything else has drifted:

| Concern | Live list | Archived page |
|---|---|---|
| Bulk action bar | `SessionList.tsx:27-72` | `ArchivedManager.tsx:179-216` |
| Shift-click range select + stale-id prune | `SessionList.tsx:158-197` | `ArchivedManager.tsx:252-299` |
| Checkbox class constant | `SessionList.tsx:12` | `ArchivedManager.tsx:36` |
| Row | `SessionCard` (rail, menu, swipe, relTime) | bare divs, `toLocaleString`, three inline buttons |
| Delete confirm | `components/PermanentDeleteDialog.tsx` | inline `PermanentDeleteDialog` with progress |
| Empty states | dashed panel + `EmptyState` | inline `<p>` + Clear link |

Archived is also a top-level nav destination, which means the archived list and the live
list can never be seen side by side and the reader (`/archived/:id`) has no list beside it
on desktop.

## 2. Goal

One list surface for sessions, live or archived, with an **Active / Archived** scope switch
in the list column. Archived rows render through the same card chrome, list chrome, bulk
selection and dialogs as live rows. The `/archived` nav entry goes away. The archived
transcript reader stays a separate, runtime-less page but now sits inside the Sessions
section so the list stays beside it on desktop.

Non-goals: merging the two data streams (the live poll must never carry archived rows),
adding `archived` to the live `SessionStatus` union, changing archive semantics on the
server, keyboard-cursor integration between the prompt bar and archived rows.

## 3. Decisions

- **D1 Scope is route-derived, not query-param or local state.** `/sessions` and
  `/sessions/:id` are the live scope; `/sessions/archived` and
  `/sessions/archived/:archivedId` are the archived scope. No `?scope=` (the shell has a
  history of search params leaking onto unrelated URLs) and no localStorage (a bookmark or
  the back button must land on the scope the user was in).
- **D2 Reader lives under the Sessions section.** `navDefForPath` resolves
  `/sessions/archived/…` to `sessions`, so `ContextList` shows the list column beside the
  reader on desktop. The route param is named `archivedId`, not `id`, so
  `useOpenSessionId()` (which reads `id`) stays null and `PageHost` mounts no
  `SessionRuntimeProvider`. This reuses the existing "param name is load-bearing" contract
  in `sessionNavState.ts` rather than adding a second gate.
- **D3 One "what item is open" reader.** `AppShell` currently derives `itemOpen` from
  `useParams().id` alone; with D2 the phone would keep the list up over the reader. A single
  `useOpenItem()` in `shell/openSessionId.ts` returns `{ kind: 'session' | 'archived', id } |
  null`; `useOpenSessionId()` becomes `useOpenItem()?.kind === 'session' ? id : null` and
  `AppShell` uses `useOpenItem() !== null` for `itemOpen`. Two consumers, one derivation.
- **D4 Separate queries, shared components.** Archived scope reads `useArchivedSessions()`
  (30 s refetch) and `useSessionSearch('archived')`; live scope is untouched. Nothing about
  the 3–5 s live poll or the WebSocket reconcile changes.
- **D5 `ArchivedSession` stays a distinct type.** `SessionCard` is not widened to a union.
  Instead the card *chrome* is extracted and both `SessionCard` and a new
  `ArchivedSessionCard` compose it. `INACTIVE_SESSION_STATUSES` and the TS status union are
  unchanged, so the mirror-zombie guard (#1033, #898) is untouched.
- **D6 Archived-only affordances stay archived-only.** Quick purge (25/50/100/200 oldest),
  bulk-delete progress + cancel, select-all header, and the Review action are rendered only
  in archived scope, as content of the shared list frame, not as new props on the live path.
- **D7 `/archived` and `/archived/:id` become redirects** (`/sessions/archived`,
  `/sessions/archived/:id`), carrying the search string so `?kiosk=1` survives, the same
  shape as the existing `/review/:sessionId` redirect. `ARCHIVED_PATH` and
  `archivedSessionPath()` keep their names and change value, so the ~6 call sites
  (`useArchiveAdvance`, `AutomationsTab` via `routes.tsx`, `ReviewView` back) need no edit.
- **D8 Fix `archivedAt` while we are here.** `list_archived` serves `archivedAt` from
  `updated_at`, which is bumped by every incidental row write. Serve `archived_at` and fall
  back to `updated_at` only when the column is NULL (rows archived before it existed). The
  "Archived 3d ago" label and the archived-date sort both read this field.

## 4. Architecture

### 4.1 Routes and navigation

```
/sessions                         live list (phone: list; desktop: list + WelcomePane)
/sessions/:id                     live session (unchanged)
/sessions/:id/agents/:agentId     sub-agent (unchanged)
/sessions/archived                archived list (phone: list; desktop: list + ArchivedWelcome)
/sessions/archived/:archivedId    reader (ReviewView), list beside it on desktop
/archived            → redirect   /sessions/archived            (keeps ?search)
/archived/:id        → redirect   /sessions/archived/:id
/review/:sessionId   → redirect   (unchanged, now chains to the above)
```

`sessionNavState.ts` gains `ARCHIVED_ROUTE = `${SESSIONS_PATH}/archived``,
`ARCHIVED_SESSION_ROUTE = `${ARCHIVED_ROUTE}/:archivedId``, and
`listScopeForPath(pathname): 'live' | 'archived'`. `ARCHIVED_PATH` is re-pointed at
`ARCHIVED_ROUTE`; `archivedSessionPath(id)` builds on it.

Route ordering: `/sessions/archived` is a static segment and ranks above `/sessions/:id` in
react-router's matcher; a test pins that a session whose id is literally `archived` cannot
exist (ids are UUIDs) and that the static route wins.

`navDefs.ts`: remove the `archived` entry and the `'archived'` badge key. The Sessions
entry's `help` text gains a clause about the Archived scope. `NavId` shrinks by one, which
`navDefs.test.ts`, `useNavBadges.test.tsx`, `NavRail.test.tsx` and `kiosk.integration.test.tsx`
must follow.

`useNavBadges`: drop `archived`. The archived count moves to the scope segment (below) via
`useArchivedSessions().data.length`, the same query the badge used, so no new fetch.

### 4.2 Shell plumbing (`web/src/shell/`)

- `openSessionId.ts` → adds `useOpenItem()` (D3). `useOpenSessionId()` remains the exported
  name every current consumer uses.
- `AppShell.tsx` → `itemOpen: useOpenItem() !== null`. `useSessionsWorkspace` (prompt-bar
  cursor, archive-advance) keeps operating on the live state only.
- `regions/ContextList.tsx` → unchanged. It already renders `SessionListColumn` whenever the
  section `hasList`; scope handling is inside the column.
- `regions/PageHost.tsx` → unchanged. Its runtime gate goes through `useOpenSessionId()`.

### 4.3 The list column (`web/src/features/sessions/`)

`SessionListColumn` becomes the scope switch point:

```
SessionListColumn
├─ head (fixed)
│  ├─ New Session button                  (live scope only)
│  ├─ ScopeSegment  [Active n | Archived m]   ← new, navigates /sessions ⇄ /sessions/archived
│  └─ SessionListControls                 (live: LIVE options + filters; archived: ARCHIVED options, no filters)
├─ scroll band
│  ├─ live:     PendingApprovalSection, QueuedSection, SessionList, AdoptPanel   (unchanged)
│  └─ archived: ArchivedSessionList                                              (new)
└─ foot: DaemonHealthIndicator            (both)
```

- `ScopeSegment` (`features/sessions/ScopeSegment.tsx`): a two-option `role="tablist"`
  control. Labels carry counts (`Active 7`, `Archived 132`). Selecting navigates; it never
  holds state of its own. Hidden in kiosk (kiosk refuses dialogs; archived actions open
  one) — same reasoning that kept `showInKiosk: false` on the old nav entry.
- `useSessionListState` stays live-only. A sibling `useArchivedListState()` returns
  `{ sessions, search, ordering }` from `useArchivedSessions()` + `useSessionSearch('archived')`
  + `useItemOrdering(…, ARCHIVED_SPEC, …)`. It is called inside `SessionListColumn` only when
  scope is archived (a small `ArchivedScope` child component so the hook is not
  conditionally called). The shell never needs the archived handle (non-goal: no prompt-bar
  cursor over archived rows).
- `ArchivedSessionList` (`features/sessions/ArchivedSessionList.tsx`): the body of today's
  `ArchivedManager` minus everything that moved into shared pieces. Owns: unarchive /
  permanent-delete / bulk mutations, select-all header, `QuickPurgeBar`, the bulk bar in
  its archived configuration, the summary line, and the single/bulk delete dialog. Renders
  rows with `ArchivedSessionCard` through the shared list frame.

### 4.4 Shared list chrome (`web/src/components/list/`, new)

Extracted from `SessionList` and `ArchivedManager`, both of which are rewritten onto them in
the same PR (no half-dead code):

| Module | Contents | Replaces |
|---|---|---|
| `useBulkSelection.ts` | `selectMode`, `selected: Set`, `toggleOne(id, shift)`, `toggleAll(ids)`, `clear()`; anchor + shift-range; stale-id prune done at render time | both copies |
| `BulkActionBar.tsx` | `region[aria-label="Bulk actions"]`, `{count} selected` label, `children` for the action buttons, `onClear` | both copies |
| `SelectableRow.tsx` | checkbox + `shiftHeld` click capture + `aria-label="Select {name}"`, wraps any row | `renderCard` select branch, `renderArchivedRow` checkbox |
| `SELECT_CHECKBOX_CLS` | one constant, exported from `SelectableRow.tsx` | both constants |
| `SkeletonCard.tsx` | as today | inline in `SessionList` |
| `NoMatchState.tsx` | dashed panel, message prop, `onClear` | live dashed panel + archived `<p>` + Clear |

`PermanentDeleteDialog` (`components/PermanentDeleteDialog.tsx`) absorbs the inline archived
one: `names: string[]` (1 = single copy, >1 = bulk copy), optional `progress` +
`onCancel`. The live callers pass `[name]`. The inline copy in `ArchivedManager` is deleted.

### 4.5 Shared card chrome (`web/src/components/card/`, new)

`SessionCard` is 847 lines; the archived card needs its *frame*, not its content. Extract:

- `CardFrame.tsx` — the `<article>` with tier rail slot, `selected` ring, list-mode focus
  (`listMode` / `onNavigate` / `onExit` keyboard handling), swipe wiring
  (`useSwipeAction` with `onSwipeRight` / `onSwipeLeft` labels + handlers), and the
  open-on-click fallthrough. Takes `rail: TierAccent | 'muted'`.
- `CardMenu.tsx` — the ⋯ trigger, popup, Escape handling, `z-20` note, `menuItem` style,
  `role="menu"`; children are the items.
- `Sep`, `actionBtn` move here and are re-exported to `SessionCard` (no duplicate string).

`SessionCard` is rewritten onto them with **no behaviour change**; its existing tests are
the regression net. `ArchivedSessionCard` (`components/ArchivedSessionCard.tsx`) then is:

```
CardFrame rail="muted"  swipeRight=Unarchive  swipeLeft=Delete  onOpen=review
  line 1: name (displayNameOf) · CardMenu [Review · Unarchive · Delete permanently]
  line 2: repo label · account · "Archived {relTime(archivedAt)}"   (relativeTime.ts, useNow)
```

No status pill, model chip, PR list, sub-agents, quick replies or autopilot marker: those
are live concepts and `ArchivedSession` has no fields for them.

### 4.6 Backend

`orchestrator.list_archived()`: `"archivedAt": iso_utc(row.archived_at or row.updated_at)`
(D8). The `updated_at` docstring in `models.py` that says it feeds the archived list is
updated to say it is the legacy fallback. No schema change, no API shape change.

## 5. Data flow

```
route pathname ──listScopeForPath──▶ SessionListColumn
                                        │
              live ◀────────────────────┴────────────────────▶ archived
  useSessionListState (unchanged)                     useArchivedListState
  GET /sessions (3–5 s poll, WS reconcile)            GET /sessions/archived (30 s)
  useSessionSearch('live')                            useSessionSearch('archived')
  LIVE_SPEC ordering + filters                        ARCHIVED_SPEC ordering, no filters
  SessionList → SessionCard                           ArchivedSessionList → ArchivedSessionCard
  onOpen → /sessions/:id                              onOpen → /sessions/archived/:archivedId
                                                      unarchive → invalidates QK.archived + QK.sessions
                                                      (row disappears here, appears in Active)
```

Unarchive from the reader (`ReviewView` header) keeps its existing behaviour; on success it
navigates to `sessionPath(id)`, which flips the scope to live because the route did.

## 6. Error handling

Unchanged mutations, unchanged toasts: unarchive and delete failures surface through the
existing hooks' `onError`. The bulk-delete progress dialog keeps its cancel path. A search
error yields `matchedIds = null` (full list, never a false "no matches"), as today.
`useArchivedSessions` failing renders the archived scope's `NoMatchState` variant "Archived
sessions unavailable" with a retry, instead of an empty list that reads as "nothing
archived" — a new state, because the old page had no error state at all.

## 7. Testing

- **Moved, not lost:** every behaviour pinned by `ArchivedTab.test.tsx` and
  `ArchivedManager.test.tsx` (rows, Review/Unarchive/Delete, dialog confirm/cancel/Escape,
  bulk bar, select-all, shift-range, Clear, quick purge thresholds, bulk-delete
  progress/cancel, collapse recoverability, group-by Account, search no-match) is
  re-homed on `ArchivedSessionList` / `ArchivedSessionCard` with the same assertions.
- **New:** `useBulkSelection` unit tests (range, anchor reset, prune); `CardFrame` /
  `CardMenu` tests (Escape closes without detaching, swipe labels); `ScopeSegment`
  navigation + counts; `listScopeForPath`; `useOpenItem` (three route shapes);
  `SessionListColumn` scope switch (live sections absent in archived scope, New Session
  absent, controls swap option sets); redirects for `/archived`, `/archived/:id`,
  `/review/:id` with search preserved; `PageHost` still mounts no runtime on
  `/sessions/archived/:archivedId`; phone hides the list on that route.
- **Updated guards:** `dedupeMobileDesktop.test.ts` — `ArchivedManager` assertions become
  "`<ArchivedSessionList` is mounted only from `SessionListColumn`" and the re-inline
  guard adds `ArchivedSessionCard`; `navDefs.test.ts`, `useNavBadges.test.tsx`,
  `NavRail.test.tsx`, `routes.test.tsx`, `kiosk.integration.test.tsx` lose their `archived`
  section expectations.
- **Backend:** `test_archived_at.py` gains a case where `updated_at` is bumped after archive
  and `archivedAt` does not move, plus the NULL-column fallback.
- Full `vitest run` every time (a hook added to a shared component breaks manual mocks in
  distant Dashboard tests — see memory), `tsc --noEmit`, `eslint`, and the four-step
  `lint-and-test` gate.

## 8. Delivery — three PRs

1. **Shared list chrome.** §4.4 modules; `SessionList` and `ArchivedManager` rewritten onto
   them; `PermanentDeleteDialog` unified; inline copies deleted. No visible change.
2. **Shared card chrome + archived card.** §4.5 modules; `SessionCard` rewritten onto them;
   `ArchivedSessionCard` + `ArchivedSessionList` replace `ArchivedManager` (deleted), still
   mounted from the `/archived` page; D8 backend fix. Visible change: archived rows look and
   behave like session cards.
3. **Scope switch.** §4.1–4.3: routes, redirects, `useOpenItem`, `ScopeSegment`,
   `useArchivedListState`, nav entry and badge removal, guard-test updates. Visible change:
   Archived leaves the nav and appears inside Sessions.

Each PR is independently shippable and leaves no dead code behind.

## 9. Docs to touch

`docs/audits/2026-09-02-ui-framework-audit.md` classes Archived as a "Standard" page; add a
note that it is now a scope of the Sessions list and the reader remains "Reader".
`docs/superpowers/plans/2026-09-03-ui-shell-appshell.md` DEC-6 (review at `/archived/:id`)
is superseded by D2 here; annotate rather than rewrite.
