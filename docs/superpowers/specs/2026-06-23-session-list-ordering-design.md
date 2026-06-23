# Session list ordering — group & sort

**Date:** 2026-06-23
**Status:** Approved (design), pending implementation plan
**Scope:** Frontend only (`web/`). No backend or API changes. Applies to **both**
list layouts — `DesktopShell` (≥1024px) and `Dashboard` (<1024px) — via a shared
hook.

## Problem

The dashboard sessions list renders in a single fixed order (status tier, via
`sortSessionsByTier`). Users want to reorganize it: group by repo, group by
status, and sort by age — without losing the current "action-required first"
default.

## Goals

- Add two **orthogonal** controls above the sessions list:
  - **Group by:** `None` (default) · `Repo` · `Status`
  - **Sort by:** `Status tier` (default) · `Last active` · `Age` · `Name`
- Sort applies *within* each group when grouping is active.
- Persist the user's selection across reloads (localStorage).
- **Default must be a visual no-op:** `group=None, sort=Status tier` reproduces
  today's ordering exactly.

## Non-goals

- No backend/API changes — `status`, `repo`, `updatedAt`, `createdAt` are all
  already on the `Session` payload.
- No new filtering (the existing tabs Sessions/Accounts/Archived are unchanged).
- No saved/named views beyond the single persisted group+sort selection.

## Architecture

### New pure module: `web/src/session/ordering.ts`

Single source of truth for grouping + sorting. Pure, deterministic, unit-tested.

```ts
export type GroupBy = 'none' | 'repo' | 'status'
export type SortBy  = 'status' | 'lastActive' | 'age' | 'name'

export interface SessionGroup {
  key: string              // stable group identity (e.g. repo nameWithOwner, tier, or 'all')
  label: string | null     // header text; null => render no header (ungrouped)
  count: number            // sessions.length, for the header chip
  sessions: Session[]
}

export function orderSessions(
  sessions: readonly Session[],
  groupBy: GroupBy,
  sortBy: SortBy,
): SessionGroup[]
```

**Reuse (no parallel implementations):**
- Status ordering → `TIER_RANK` / `statusTier` from `session/statusTier.ts`.
- Repo label + Scratch bucket → `formatRepoLabel` from `lib/repoLabel.ts`.
- Last-active validity → `isValidTimestamp` from `lib/relativeTime.ts`.

**Group ordering:**
- `status`: groups ordered by `TIER_RANK` (needs-you → error → your-turn → busy).
- `repo`: groups alphabetical by label, case-insensitive, with **Scratch last**.
- `none`: a single group `{ key: 'all', label: null, … }`.

**Within-group sort (`SortBy` comparators):**
- `status`: `TIER_RANK[statusTier(a.status)]` ascending.
- `lastActive`: `updatedAt` descending (newest first); null/invalid
  (`!isValidTimestamp`) sinks to the bottom.
- `age`: `createdAt` ascending (oldest created first).
- `name`: `localeCompare`, case-insensitive.
- **Every comparator ends with a deterministic tie-break on `id`** so a React
  Query refetch never reorders equal rows (no visual jitter).

**Parity invariant (pinned by test):**
`orderSessions(s, 'none', 'status').flatMap(g => g.sessions)` deep-equals
`sortSessionsByTier(s)` for any input. This guarantees the default is a no-op.

### Shared hook: `web/src/session/useSessionOrdering.ts`

The app has **two parallel list layouts** (`App.tsx` hard-switches at
`min-width: 1024px`): `DesktopShell` (≥1024px sidebar) and the routed
`Dashboard` (<1024px). To keep one source of truth, the group/sort state +
persistence + derivation live in a shared hook consumed by both:

```ts
interface SessionOrdering {
  groupBy: GroupBy
  sortBy: SortBy
  setGroupBy: (g: GroupBy) => void
  setSortBy: (s: SortBy) => void
  groups: SessionGroup[]           // for SessionList rendering
  orderedSessions: Session[]       // groups flattened — for keyboard nav + selection
}
export function useSessionOrdering(sessions: readonly Session[]): SessionOrdering
```

- Initializes `groupBy`/`sortBy` from `loadSessionOrder()`; writes via a
  `useEffect` on change (`saveSessionOrder`).
- `groups = useMemo(() => orderSessions(sessions, groupBy, sortBy), …)`;
  `orderedSessions = useMemo(() => groups.flatMap(g => g.sessions), [groups])`.

### `Dashboard.tsx`

Replace the single line

```ts
const orderedSessions = useMemo(() => sortSessionsByTier(sessions), [sessions])
```

with the hook:

```ts
const { groupBy, sortBy, setGroupBy, setSortBy, groups, orderedSessions } = useSessionOrdering(sessions)
```

`orderedSessions` (the flattened sequence) continues to feed `useGlobalListNav`
and selection, so keyboard navigation and `aria-activedescendant` are unaffected
— group headers never enter the arrow-key order. `Dashboard` renders
`<SessionListControls>` above the list and passes `groups` to `SessionList`.

### `DesktopShell.tsx`

Same hook. `DesktopShell` currently renders sessions in **raw `useSessions()`
order** (it never applied `sortSessionsByTier`). After this change it consumes
`useSessionOrdering(sessions)` and uses `orderedSessions` for its keyboard nav
(`highlighted`, `clampedIndex`, the index→session lookups at lines ~134/147/282)
and `groups` for the list. **Intended behavior change:** the desktop sidebar's
default order becomes status-tier (matching `Dashboard`) instead of raw — a
consistency improvement, not a regression. Controls render above the sidebar
list (next to the "New Session" button area).

### `lib/sessionOrder.ts`

Tiny persistence helper mirroring `lib/lastRepo.ts`:

```ts
const KEY = 'ccswitch.sessionOrder'
export function loadSessionOrder(): { groupBy: GroupBy; sortBy: SortBy }  // defaults: none / status
export function saveSessionOrder(v: { groupBy: GroupBy; sortBy: SortBy }): void
```

Reads tolerate missing/corrupt JSON and unknown enum values by falling back to
the defaults. Dashboard writes via a `useEffect` on `[groupBy, sortBy]`.

### `SessionListControls.tsx`

Two native `<select>` elements (house style — matches `Dashboard`,
`DispatchBar`, `SettingsPanel`; there is no shadcn Select primitive), each with
an associated `<label>`. Props: `{ groupBy, sortBy, onGroupByChange, onSortByChange }`.
Rendered above `SessionList` in the Sessions tab, only when sessions exist.

### `SessionList.tsx`

Add an **optional** `groups?: SessionGroup[]` prop (additive — the existing
flat `sessions` prop and all ~20 current call sites stay valid, so no churn in
`a11y.test.tsx`, the existing `SessionList.test.tsx` cases, etc.). When `groups`
is provided, render from it; otherwise fall back to the flat `sessions` path
(today's behavior). For each group, render a sticky, **non-interactive** header
(`role="presentation"`, `aria-hidden`) showing `label` + `count` when
`label !== null`; render nothing when `label === null` (ungrouped →
pixel-identical to today). Rows render in the same flat order as
`orderedSessions`.

## Edge cases

- **Empty / loading:** unchanged — `EmptyState` / skeletons render before any
  group logic; controls are hidden when there are no sessions.
- **null `repo`** → grouped under "Scratch" (via `formatRepoLabel`), sorted last.
- **null/invalid `updatedAt`** → sinks to the bottom under Last-active sort;
  never crashes a comparator.
- **Sub-agent expansion & scroll-into-view:** untouched (per-card concern).
- **Keyboard nav across group boundaries:** works because nav consumes the flat
  `orderedSessions`, not the grouped structure.

## Trade-off (accepted)

When a non-status sort (Last active / Age / Name) is chosen **without**
grouping, action-required (needs-you) sessions are no longer pinned to the top.
This is the expected, user-chosen behavior.

## Testing (TDD)

- `session/ordering.test.ts`
  - each comparator (status, lastActive desc + null sink, age asc, name)
  - group ordering: status via `TIER_RANK`; repo alphabetical with Scratch last
  - within-group sort correctness
  - deterministic tie-break (equal rows keep input-id order)
  - **parity invariant** vs `sortSessionsByTier`
- `components/__tests__/SessionList.test.tsx`
  - headers + counts rendered when `groups` provided; no header when ungrouped
  - existing flat-`sessions` cases still pass unchanged (additive prop)
- `session/useSessionOrdering.test.ts(x)`: initializes from + persists to
  localStorage; `setGroupBy`/`setSortBy` update `groups`/`orderedSessions`;
  `orderedSessions` equals `groups` flattened
- `SessionListControls` test: changing a select calls the handler
- keyboard-nav test: arrow keys traverse across a group boundary (Dashboard
  and/or DesktopShell)

## Reuse checklist (constitution VI)

| Concept            | Existing source of truth          | Action      |
|--------------------|-----------------------------------|-------------|
| Status order       | `session/statusTier.ts` `TIER_RANK` | reuse     |
| Repo label/Scratch | `lib/repoLabel.ts` `formatRepoLabel`| reuse     |
| Timestamp validity | `lib/relativeTime.ts` `isValidTimestamp` | reuse |
| Persisted UI pref  | `lib/lastRepo.ts` pattern         | mirror      |
| Select control     | native `<select>` (house style)   | match       |
