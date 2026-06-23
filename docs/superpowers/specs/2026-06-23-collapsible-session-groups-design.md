# Collapsible Session Groups — Design

**Date:** 2026-06-23
**Status:** Approved (design), pending implementation
**Feature area:** Web frontend — session list grouping

## Problem

The session list can group sessions **By status** or **By repo** (selected via the
order menu; `groupBy` in `useSessionOrdering`). Each group renders a sticky header
(label + count) followed by every session card. With many sessions or many repos the
list gets long and there's no way to fold a group you don't care about right now.

Add the ability to expand/collapse each group, so users can hide the cards under a
group header while keeping the header (and its count) visible.

## Decisions (approved)

1. **Persistence:** persist collapsed state to `localStorage`, mirroring the existing
   `lib/sessionOrder.ts` pattern.
2. **Default + bulk control:** all groups start expanded; toggle each header
   individually. No collapse-all/expand-all control in v1.
3. **Collapsed header look:** keep label + count visible (e.g. `Working 3`).
4. **Scope:** collapse applies only when grouping is active (`By status` / `By repo`).
   In `None` mode there is a single unlabeled group (`label === null`) and no header,
   so nothing changes there.

## Approach

Store the **set of collapsed group keys** (not expanded keys). An empty set means
everything is expanded, so "default expanded" needs no special-casing and any newly
appearing group (a new repo, a tier that just gained its first session) is expanded by
default. Groups are keyed by the stable `group.key` already produced by
`orderSessions` (status → tier name; repo → repo label). Keys do not collide across
modes, and a stale key from a different grouping mode simply matches no current group —
harmless — so a single shared `Set<string>` works for both modes.

### Components & data flow

```
lib/groupCollapse.ts        load/save Set<string> ⇄ localStorage  (new)
  └─ useSessionOrdering.ts   owns collapsedKeys state + persistence + toggle  (edit)
       ├─ groups            unchanged: full sessions + real count
       ├─ collapsedKeys     Set<string> of collapsed group keys
       ├─ toggleCollapse(key)
       └─ orderedSessions   flatMap of sessions in NON-collapsed groups  (edit)
            └─ DispatchBar keyboard nav skips hidden rows automatically
  └─ SessionList.tsx        header → disclosure button; hides cards when collapsed (edit)
       └─ threaded through both consumers (DesktopShell + Dashboard)
```

### `lib/groupCollapse.ts` (new)

Mirror `sessionOrder.ts`: try/catch-wrapped, best-effort, degrades to "nothing
collapsed" if `localStorage` is unavailable or the payload is malformed.

- `KEY = 'ccswitch.groupCollapse'`
- Persisted shape: a JSON `string[]` of collapsed keys (sets don't serialize directly).
- `loadGroupCollapse(): Set<string>` — parse array → `Set`; non-array/parse error → empty set.
- `saveGroupCollapse(keys: Set<string>): void` — `JSON.stringify([...keys])`.

### `useSessionOrdering.ts` (edit)

- `const [collapsedKeys, setCollapsedKeys] = useState<Set<string>>(loadGroupCollapse)`.
- `useEffect` persists `collapsedKeys` via `saveGroupCollapse` (same shape as the
  existing `saveSessionOrder` effect).
- `toggleCollapse(key: string)` — returns a new `Set` with `key` added/removed (new
  reference so React re-renders; never mutate state in place).
- `orderedSessions` changes to flatten only non-collapsed groups:
  `groups.filter(g => !collapsedKeys.has(g.key)).flatMap(g => g.sessions)`.
  This keeps `DispatchBar` arrow-key navigation from landing on hidden rows.
- Extend the `SessionOrdering` interface with `collapsedKeys` and `toggleCollapse`.

### `SessionList.tsx` (edit)

Replace the decorative header (`role="presentation"` + `aria-hidden="true"`) with a
disclosure **button** — the standard accessible expand/collapse pattern:

- `<button type="button" aria-expanded={!collapsed} aria-controls={panelId}>` wrapping a
  chevron icon + label + count. Keep the sticky styling. The button is keyboard-reachable
  and screen-reader-labelled; it is **not** `aria-hidden`.
- Chevron rotates (right when collapsed, down when expanded), consistent with the
  existing chevron-toggle in `SessionCard.tsx`.
- `panelId` = stable id derived from `group.key`; the cards container gets `id={panelId}`.
- Render `group.sessions.map(renderCard)` only when the group is **not** collapsed.
- New optional props: `collapsedKeys?: Set<string>` and `onToggleCollapse?: (key) => void`.
  When omitted (e.g. flat/`None` rendering), behaviour is unchanged.

### Consumers

`useSessionOrdering` is used by both layouts (DesktopShell ≥1024px and Dashboard
<1024px). Both already pass `groups` to `SessionList`; thread `collapsedKeys` and
`onToggleCollapse` through the same way so behaviour stays consistent across breakpoints.

## Accessibility

- Header becomes a native `<button>` disclosure widget: `aria-expanded` reflects state,
  `aria-controls` points at the cards panel. This is the WAI-ARIA disclosure pattern and
  is keyboard-operable (Enter/Space) for free.
- The list container keeps its `aria-activedescendant` virtual-focus model. Header
  buttons are real focusable controls in tab order — consistent with the existing
  per-card overflow-menu buttons, which are already real buttons inside the same list.
- Collapsed sessions leave `orderedSessions`, so virtual focus never points at a hidden row.

## Error handling / edge cases

- `localStorage` blocked/unavailable → load returns empty set, save is a no-op (try/catch).
- Malformed stored payload → empty set (treated as all-expanded).
- Stale keys (group no longer present, or from the other grouping mode) → ignored; they
  match no current group. We do **not** prune them on load — harmless and avoids coupling
  storage to the current session set. They get overwritten on the next toggle.
- `None` mode / flat render → no headers, props unused, unchanged.

## Testing

Vitest (web):
- `groupCollapse.ts`: round-trips a set; empty/malformed/blocked storage → empty set;
  save serializes to a string array.
- `useSessionOrdering`: `toggleCollapse` adds/removes a key and returns a new set;
  `orderedSessions` excludes sessions in collapsed groups; collapsed state persists
  across a remount (load reads what save wrote).
- `SessionList`: collapsed group hides its cards but keeps header + count; header button
  exposes `aria-expanded` and toggles it; clicking calls `onToggleCollapse` with the
  group key; flat/`None` render is unaffected.

Gates: `vitest run`, `tsc -b --force` (the real type gate — `tsc --noEmit` checks
nothing here), `eslint`.

## Out of scope (YAGNI)

- Collapse-all / expand-all bulk control.
- Per-account or server-side persistence of collapse state.
- Animated height transitions (instant show/hide in v1).
- Collapse in `None` mode.
