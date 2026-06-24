# Group-header keyboard navigation — design

**Date:** 2026-06-24
**Status:** Approved (design)
**Area:** web frontend — session list keyboard navigation

## Problem

While navigating the session list with `↑`/`↓`, the keyboard cursor moves only
through session rows. Group headers (rendered when `groupBy` is `status` or
`repo`) are clickable but unreachable from the keyboard, so a keyboard user can
neither land on a header nor collapse/expand a group without the mouse.

We want: `↑`/`↓` also stops on group headers, and when the cursor is on a header,
`←`/`→` collapse/expand that group.

## Current architecture (as-is)

- **Cursor:** a single `highlightedIndex: number` held *separately* in
  `DesktopShell.tsx` (≥1024px) and `Dashboard.tsx` (<1024px). It indexes into
  `orderedSessions`.
- **`orderedSessions`** (`web/src/session/useSessionOrdering.ts:59`) is a flat
  list of **sessions only**, already filtered to drop sessions in collapsed
  groups: `groups.filter(g => !collapsedKeys.has(g.key)).flatMap(g => g.sessions)`.
  Headers are not in it, so the cursor can never point at one.
- **Grouping** (`web/src/session/ordering.ts`): `SessionGroup = {key, label, count,
  sessions}`. `label` is `null` only when `groupBy === 'none'` (single anonymous
  group) — that is the one case where no header is rendered.
- **Collapse state:** `collapsedKeys: Set<string>` + `toggleCollapse(key)` live in
  `useSessionOrdering`, persisted to localStorage.
- **List-mode keys** (`web/src/lib/shortcuts.ts`): on a focused **session** row,
  `→`/`Enter`/`Space` = open, `←`/`Esc`/`Tab` = exit to compose, `↑`/`↓` =
  navigate. `matchListShortcut(e)` is the single source mapping keys→actions; the
  same table drives the help panel.
- **Two focus modes:**
  - *Compose mode* — real focus in the DispatchBar textarea. `↑`/`↓` at a line
    edge (or empty prompt) call `onNavigate`; the highlight is shown virtually via
    `aria-activedescendant`. Empty prompt + `→` opens the highlighted session
    (`DispatchBar.tsx:149`). Empty prompt + `Tab` enters list mode.
  - *List-focus mode* — the highlighted row's `<button>` takes **real** DOM focus
    (`SessionCard.tsx:71`), so single-key shortcuts land on it via
    `handleKeyDown` → `matchListShortcut`.

## Decisions (from brainstorming)

1. **Header `←`/`→` semantics: simple collapse/expand.** `→` expands (no-op if
   already open), `←` collapses (no-op if already closed). The cursor stays on the
   header. No tree-style dive-into-children / jump-to-parent.
2. **Modes: both.** Works in list-focus mode always, and in compose mode when the
   prompt is **empty** (reusing the existing empty-prompt guard so caret movement
   never regresses). With a non-empty prompt, `←`/`→` move the caret as today.
3. **`Enter`/`Space` on a header: toggle collapse** (matches the header's existing
   click behavior).
4. **Session-row behavior is unchanged.** `←`/`→`/`Enter` keep their current
   meaning when the cursor is on a session.

## Design

### Navigation model

Introduce a unified ordered list of navigable items:

```ts
type NavItem =
  | { kind: 'header'; key: string; label: string; count: number; collapsed: boolean }
  | { kind: 'session'; session: Session }
```

Built by walking `groups`: for each group, push a `header` item **only when
`label !== null`**, then push each session as a `session` item **only when the
group is not collapsed**. Consequences:

- `groupBy === 'none'` → zero headers → `navItems` is exactly today's
  `orderedSessions` wrapped as session items. The ungrouped view is unchanged.
- A collapsed group contributes its header but none of its sessions, so the cursor
  can sit on a collapsed header and expand it.

`orderedSessions` is retained (still the flat session list) for the consumers that
need session-only data (jump-by-number, clamping callers that operate on sessions,
counts). `navItems` is the new cursor domain.

### `useListNavigation` (new shared hook)

To avoid writing the header-aware cursor logic twice, extract the cursor into a
small hook consumed by both layouts:

```ts
function useListNavigation(navItems: NavItem[]): {
  highlightedIndex: number
  setHighlightedIndex: (i: number) => void
  highlighted: NavItem | undefined
  highlightedSession: Session | undefined   // session iff highlighted.kind === 'session'
  highlightedGroupKey: string | undefined   // key iff highlighted.kind === 'header'
  navigate: (dir: 'up' | 'down') => void     // clamps to [0, navItems.length-1]
}
```

`navigate` simply steps the index by ±1 within bounds (every nav item — header or
session — is a stop). Clamp-on-shrink logic (today at `DesktopShell.tsx:149`)
moves into the hook and clamps against `navItems.length`.

The open/jump/exit/dispatch handlers that genuinely differ between layouts stay in
each layout; they read `highlightedSession` instead of `orderedSessions[i]`.
`detachToList` re-derives the index by finding the session's `navItems` position.

### Keybindings (`shortcuts.ts`)

Keep keys in one place. Add header actions and a header matcher:

```ts
type ListRowAction = … | { type: 'expand' } | { type: 'collapse' } | { type: 'toggle' }

matchHeaderShortcut(e): ListRowAction | undefined
//  → expand        Enter/Space → toggle
//  ← collapse       ↑/↓ → navigate(up|down)
//  Esc/Tab → exit
```

Add header entries to the `list-mode` help group so the `?` panel documents the
new keys.

### `SessionList` / header rendering

The group-header `<button>` becomes a cursor target:

- New props: `selectedGroupKey?: string`, `onNavigate?`, `onExit?`,
  `onExpand?(key)`, `onCollapse?(key)` (directional, idempotent), reusing the
  existing `onToggleCollapse(key)` for the `Enter`/`Space` toggle.
- Apply the same `selected` highlight style rows use (`ring-2 ring-blue-500 …`)
  when `group.key === selectedGroupKey`.
- Give the header a stable `id` (e.g. `group-header-<key>`) for
  `aria-activedescendant` in compose mode.
- In **list-focus mode**, when the header is the cursor target, take real DOM
  focus (a `ref` + `useEffect`, mirroring `SessionCard.tsx:70`) and handle
  `keydown` through `matchHeaderShortcut`, dispatching to the new callbacks.

`aria-activedescendant` on the list container (`SessionList.tsx:134`) points to
the header id when a header is highlighted, else the row id.

### DispatchBar (compose mode)

When the prompt is **empty** and the highlight is a **header**:

- `→` → expand, `←` → collapse, `Enter`/`Space` → toggle (instead of
  open-highlighted).
- When the highlight is a **session**, behavior is unchanged (`→`/`Enter` open).

This routes through new optional callbacks (`onExpandHighlighted`,
`onCollapseHighlighted`, and a toggle) plumbed from the layout, which act on
`highlightedGroupKey`. Non-empty prompt is untouched, so caret movement and
existing shortcuts are unaffected.

## Components & boundaries

- `ordering.ts` / `useSessionOrdering.ts` — owns `navItems` derivation + collapse
  state. Pure, unit-testable.
- `useListNavigation.ts` (new) — owns the cursor over `navItems`. Pure logic,
  unit-testable.
- `SessionList.tsx` / `SessionCard.tsx` — render + per-item focus/keydown. Header
  keydown routes through `matchHeaderShortcut`.
- `shortcuts.ts` — single source for keys + help text.
- `DesktopShell.tsx` / `Dashboard.tsx` — consume `useListNavigation`; keep their
  own open/jump/exit/dispatch handlers.
- `DispatchBar.tsx` — empty-prompt header branch for `←`/`→`/`Enter`.

## Testing

- **Unit (`navItems`):** headers present/absent per `groupBy`; collapsed group
  contributes header but no sessions; `groupBy==='none'` matches `orderedSessions`.
- **Unit (`useListNavigation`):** `navigate` steps across header↔session
  boundaries; clamps at both ends; re-clamps when `navItems` shrinks.
- **Component (list mode):** focus lands on a header via `↓`; `→` expands, `←`
  collapses, `Enter` toggles; `↑`/`↓` move off; session-row keys unchanged.
- **Component (compose mode):** empty prompt + header highlighted → `←`/`→`/`Enter`
  collapse/expand; non-empty prompt → arrows move caret (no toggle).
- Both layouts (`DesktopShell`, `Dashboard`) exercise the shared hook.

## Out of scope

- Tree-style (WAI-ARIA TreeView) child/parent traversal.
- Changing collapse persistence, grouping rules, or session-row shortcuts.
- Any backend change.
