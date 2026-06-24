# Group-header Keyboard Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `↑`/`↓` keyboard navigation of the session list also stop on group headers, and let `←`/`→` collapse/expand the highlighted group (in both list-focus and compose modes).

**Architecture:** Introduce a unified `NavItem` list (headers + sessions) that the cursor indexes into, derived in `useSessionOrdering`. Extract the cursor into a shared `useListNavigation` hook consumed by both layouts (`DesktopShell`, `Dashboard`) so the header-aware logic exists once. Header keys are matched via a new `matchHeaderShortcut`; a new `GroupHeader` component renders the header, takes focus in list mode, and dispatches collapse/expand. `DispatchBar` branches its empty-prompt arrow handling when the highlight is a header.

**Tech Stack:** React + TypeScript, Vite, Vitest + @testing-library/react, Tailwind, lucide-react icons.

## Global Constraints

- Web tests run from `web/`: `node_modules/.bin/vitest run`, `node_modules/.bin/tsc -b --force`, `node_modules/.bin/eslint .`. All three must stay green. (If `node_modules` is absent: `rm -f node_modules && npm ci`.)
- Real type gate is `tsc -b` — `tsc --noEmit` checks nothing here.
- eslint runs with `--max-warnings 0` in CI; no new warnings (incl. react-hooks rules).
- Single source of truth: keybindings live in `web/src/lib/shortcuts.ts`; collapse state lives in `useSessionOrdering`. Do not add parallel copies.
- Behavior when `groupBy === 'none'` (no headers) must be byte-identical to today.
- Session-row key behavior is unchanged (`→` open, `←` exit, `Enter`/`Space` open, etc.).
- Commit after each task. Branch is `feat/group-header-keyboard-nav` (already created off `origin/master`).

---

### Task 1: `NavItem` type + `buildNavItems` (pure)

**Files:**
- Modify: `web/src/session/ordering.ts` (append after `orderSessions`)
- Test: `web/src/session/__tests__/buildNavItems.test.ts` (create)

**Interfaces:**
- Consumes: `SessionGroup` (existing, in `ordering.ts`), `Session` (`@/api/types`).
- Produces:
  ```ts
  export type NavItem =
    | { kind: 'header'; key: string; label: string; count: number; collapsed: boolean }
    | { kind: 'session'; session: Session }
  export function buildNavItems(
    groups: readonly SessionGroup[],
    collapsedKeys: ReadonlySet<string>,
  ): NavItem[]
  ```

- [ ] **Step 1: Write the failing test**

```ts
// web/src/session/__tests__/buildNavItems.test.ts
import { describe, it, expect } from 'vitest'
import { buildNavItems, type SessionGroup } from '../ordering'
import type { Session } from '@/api/types'

function mk(id: string): Session {
  return {
    id, name: id, accountName: 'a', status: 'idle',
    createdAt: '2026-06-01T00:00:00Z', updatedAt: '2026-06-01T00:00:00Z',
  }
}
const grp = (key: string, label: string | null, sessions: Session[]): SessionGroup =>
  ({ key, label, count: sessions.length, sessions })

describe('buildNavItems', () => {
  it('ungrouped (label null) yields only session items — parity with sessions list', () => {
    const groups = [grp('all', null, [mk('a'), mk('b')])]
    const items = buildNavItems(groups, new Set())
    expect(items).toEqual([
      { kind: 'session', session: groups[0].sessions[0] },
      { kind: 'session', session: groups[0].sessions[1] },
    ])
  })

  it('labelled group emits a header followed by its sessions', () => {
    const groups = [grp('busy', 'Working', [mk('a')])]
    const items = buildNavItems(groups, new Set())
    expect(items[0]).toEqual({ kind: 'header', key: 'busy', label: 'Working', count: 1, collapsed: false })
    expect(items[1]).toEqual({ kind: 'session', session: groups[0].sessions[0] })
  })

  it('collapsed group emits its header (collapsed:true) but none of its sessions', () => {
    const groups = [grp('busy', 'Working', [mk('a'), mk('b')]), grp('idle', 'Idle', [mk('c')])]
    const items = buildNavItems(groups, new Set(['busy']))
    expect(items).toEqual([
      { kind: 'header', key: 'busy', label: 'Working', count: 2, collapsed: true },
      { kind: 'header', key: 'idle', label: 'Idle', count: 1, collapsed: false },
      { kind: 'session', session: groups[1].sessions[0] },
    ])
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node_modules/.bin/vitest run src/session/__tests__/buildNavItems.test.ts`
Expected: FAIL — `buildNavItems` not exported.

- [ ] **Step 3: Write minimal implementation** (append to `web/src/session/ordering.ts`)

```ts
/** A single keyboard-navigable item in the session list: a group header or a session row. */
export type NavItem =
  | { kind: 'header'; key: string; label: string; count: number; collapsed: boolean }
  | { kind: 'session'; session: Session }

/**
 * Flatten ordered groups into the cursor's navigation domain: a labelled group
 * contributes a header item; a non-collapsed group also contributes its session
 * items. When a group has no label (groupBy='none') no header is emitted, so the
 * result is exactly the flat session list — the ungrouped view is unchanged.
 */
export function buildNavItems(
  groups: readonly SessionGroup[],
  collapsedKeys: ReadonlySet<string>,
): NavItem[] {
  const items: NavItem[] = []
  for (const g of groups) {
    const collapsed = collapsedKeys.has(g.key)
    if (g.label !== null) {
      items.push({ kind: 'header', key: g.key, label: g.label, count: g.count, collapsed })
    }
    if (!collapsed) {
      for (const s of g.sessions) items.push({ kind: 'session', session: s })
    }
  }
  return items
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node_modules/.bin/vitest run src/session/__tests__/buildNavItems.test.ts`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add web/src/session/ordering.ts web/src/session/__tests__/buildNavItems.test.ts
git commit -m "feat(web): add NavItem type + buildNavItems for header-aware nav

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Expose `navItems` + directional collapse from `useSessionOrdering`

**Files:**
- Modify: `web/src/session/useSessionOrdering.ts`
- Test: `web/src/session/__tests__/useSessionOrdering.test.ts` (add cases)

**Interfaces:**
- Consumes: `buildNavItems`, `NavItem` (Task 1).
- Produces (added to `SessionOrdering`):
  ```ts
  navItems: NavItem[]
  expandGroup: (key: string) => void
  collapseGroup: (key: string) => void
  ```
  (`toggleCollapse`, `groups`, `orderedSessions`, `collapsedKeys` remain.)

- [ ] **Step 1: Write the failing test** (append inside the existing `describe('useSessionOrdering', …)` in `web/src/session/__tests__/useSessionOrdering.test.ts`)

```ts
  it('navItems: ungrouped has no headers (one item per session)', () => {
    const { result } = renderHook(() => useSessionOrdering([mk('a'), mk('b')]))
    expect(result.current.navItems.every((i) => i.kind === 'session')).toBe(true)
    expect(result.current.navItems).toHaveLength(2)
  })

  it('navItems: grouped emits headers; collapseGroup drops that group\'s sessions', () => {
    const { result } = renderHook(() =>
      useSessionOrdering([mk('a', { repo: 'o/r1' }), mk('b', { repo: 'o/r2' })]),
    )
    act(() => result.current.setGroupBy('repo'))
    expect(result.current.navItems.filter((i) => i.kind === 'header')).toHaveLength(2)
    const firstKey = result.current.groups[0].key
    act(() => result.current.collapseGroup(firstKey))
    const header = result.current.navItems.find((i) => i.kind === 'header' && i.key === firstKey)
    expect(header && header.kind === 'header' && header.collapsed).toBe(true)
    // collapsed group contributes no session items
    expect(result.current.navItems.some(
      (i) => i.kind === 'session' && result.current.groups[0].sessions.some((s) => s.id === i.session.id),
    )).toBe(false)
  })

  it('expandGroup re-expands a collapsed group (idempotent)', () => {
    const { result } = renderHook(() => useSessionOrdering([mk('a', { repo: 'o/r1' })]))
    act(() => result.current.setGroupBy('repo'))
    const key = result.current.groups[0].key
    act(() => result.current.collapseGroup(key))
    act(() => result.current.expandGroup(key))
    act(() => result.current.expandGroup(key)) // idempotent
    expect(result.current.collapsedKeys.has(key)).toBe(false)
  })
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node_modules/.bin/vitest run src/session/__tests__/useSessionOrdering.test.ts`
Expected: FAIL — `navItems` / `collapseGroup` / `expandGroup` undefined.

- [ ] **Step 3: Write minimal implementation**

In `web/src/session/useSessionOrdering.ts`:

(a) Update the import to add the new symbols:
```ts
import { orderSessions, buildNavItems, type GroupBy, type SortBy, type SessionGroup, type NavItem } from './ordering'
```

(b) Extend the `SessionOrdering` interface:
```ts
export interface SessionOrdering {
  groupBy: GroupBy
  sortBy: SortBy
  setGroupBy: (g: GroupBy) => void
  setSortBy: (s: SortBy) => void
  groups: SessionGroup[]
  orderedSessions: Session[]
  navItems: NavItem[]
  collapsedKeys: Set<string>
  toggleCollapse: (key: string) => void
  expandGroup: (key: string) => void
  collapseGroup: (key: string) => void
}
```

(c) Add the directional setters next to `toggleCollapse`:
```ts
  const expandGroup = useCallback((key: string) => {
    setCollapsedKeys((prev) => {
      if (!prev.has(key)) return prev
      const next = new Set(prev)
      next.delete(key)
      return next
    })
  }, [])

  const collapseGroup = useCallback((key: string) => {
    setCollapsedKeys((prev) => {
      if (prev.has(key)) return prev
      const next = new Set(prev)
      next.add(key)
      return next
    })
  }, [])
```

(d) Derive `navItems` after `orderedSessions`:
```ts
  const navItems = useMemo(() => buildNavItems(groups, collapsedKeys), [groups, collapsedKeys])
```

(e) Add to the returned object: `navItems, expandGroup, collapseGroup`.

- [ ] **Step 4: Run test to verify it passes**

Run: `node_modules/.bin/vitest run src/session/__tests__/useSessionOrdering.test.ts`
Expected: PASS (existing + 3 new)

- [ ] **Step 5: Commit**

```bash
git add web/src/session/useSessionOrdering.ts web/src/session/__tests__/useSessionOrdering.test.ts
git commit -m "feat(web): expose navItems + expandGroup/collapseGroup from useSessionOrdering

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `useListNavigation` shared cursor hook

**Files:**
- Create: `web/src/session/useListNavigation.ts`
- Test: `web/src/session/__tests__/useListNavigation.test.ts` (create)

**Interfaces:**
- Consumes: `NavItem` (Task 1).
- Produces:
  ```ts
  export interface ListNavigation {
    highlightedIndex: number
    setHighlightedIndex: (i: number) => void
    highlighted: NavItem | undefined
    highlightedSession: Session | undefined
    highlightedGroupKey: string | undefined
    navigate: (dir: 'up' | 'down') => void
    highlightSession: (id: string) => void
  }
  export function useListNavigation(navItems: readonly NavItem[]): ListNavigation
  ```

- [ ] **Step 1: Write the failing test**

```ts
// web/src/session/__tests__/useListNavigation.test.ts
import { describe, it, expect } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useListNavigation } from '../useListNavigation'
import type { NavItem } from '../ordering'
import type { Session } from '@/api/types'

const sess = (id: string): Session => ({
  id, name: id, accountName: 'a', status: 'idle',
  createdAt: '2026-06-01T00:00:00Z', updatedAt: '2026-06-01T00:00:00Z',
})
// header(h1) · session(a) · header(h2) · session(b)
const items: NavItem[] = [
  { kind: 'header', key: 'h1', label: 'H1', count: 1, collapsed: false },
  { kind: 'session', session: sess('a') },
  { kind: 'header', key: 'h2', label: 'H2', count: 1, collapsed: false },
  { kind: 'session', session: sess('b') },
]

describe('useListNavigation', () => {
  it('starts unhighlighted and steps down across header↔session boundaries', () => {
    const { result } = renderHook(() => useListNavigation(items))
    expect(result.current.highlightedIndex).toBe(-1)
    act(() => result.current.navigate('down')) // -1 -> 0 (header h1)
    expect(result.current.highlightedGroupKey).toBe('h1')
    expect(result.current.highlightedSession).toBeUndefined()
    act(() => result.current.navigate('down')) // 0 -> 1 (session a)
    expect(result.current.highlightedSession?.id).toBe('a')
    expect(result.current.highlightedGroupKey).toBeUndefined()
  })

  it('clamps at both ends', () => {
    const { result } = renderHook(() => useListNavigation(items))
    act(() => result.current.navigate('up')) // stays clamped at 0
    expect(result.current.highlightedIndex).toBe(0)
    act(() => result.current.setHighlightedIndex(3))
    act(() => result.current.navigate('down')) // stays at last
    expect(result.current.highlightedIndex).toBe(3)
  })

  it('re-clamps when navItems shrinks', () => {
    const { result, rerender } = renderHook(({ it }) => useListNavigation(it), {
      initialProps: { it: items },
    })
    act(() => result.current.setHighlightedIndex(3))
    rerender({ it: items.slice(0, 2) }) // now length 2 → max index 1
    expect(result.current.highlightedIndex).toBe(1)
  })

  it('highlightSession jumps to the navItem for a session id', () => {
    const { result } = renderHook(() => useListNavigation(items))
    act(() => result.current.highlightSession('b'))
    expect(result.current.highlightedIndex).toBe(3)
    expect(result.current.highlightedSession?.id).toBe('b')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node_modules/.bin/vitest run src/session/__tests__/useListNavigation.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```ts
// web/src/session/useListNavigation.ts
import { useCallback, useState } from 'react'
import type { Session } from '@/api/types'
import type { NavItem } from './ordering'

export interface ListNavigation {
  highlightedIndex: number
  setHighlightedIndex: (i: number) => void
  highlighted: NavItem | undefined
  highlightedSession: Session | undefined
  highlightedGroupKey: string | undefined
  navigate: (dir: 'up' | 'down') => void
  highlightSession: (id: string) => void
}

/**
 * Owns the keyboard cursor over a list of NavItems (group headers + sessions).
 * Shared by both layouts (DesktopShell, Dashboard) so the header-aware logic
 * lives in one place. The index clamps to the list during render when the list
 * shrinks (mirrors the prior per-layout clamp).
 */
export function useListNavigation(navItems: readonly NavItem[]): ListNavigation {
  const [highlightedIndex, setHighlightedIndex] = useState(-1)

  // Clamp during render when the list shrinks, then derive from the clamped value.
  const clamped = navItems.length === 0 ? -1 : Math.min(highlightedIndex, navItems.length - 1)
  if (clamped !== highlightedIndex) setHighlightedIndex(clamped)

  const highlighted = clamped >= 0 ? navItems[clamped] : undefined
  const highlightedSession = highlighted?.kind === 'session' ? highlighted.session : undefined
  const highlightedGroupKey = highlighted?.kind === 'header' ? highlighted.key : undefined

  const navigate = useCallback((dir: 'up' | 'down') => {
    if (navItems.length === 0) return
    setHighlightedIndex((i) => {
      if (dir === 'up') return i <= 0 ? 0 : i - 1
      return Math.min(navItems.length - 1, i + 1)
    })
  }, [navItems.length])

  const highlightSession = useCallback((id: string) => {
    const idx = navItems.findIndex((it) => it.kind === 'session' && it.session.id === id)
    if (idx >= 0) setHighlightedIndex(idx)
  }, [navItems])

  return {
    highlightedIndex: clamped,
    setHighlightedIndex,
    highlighted,
    highlightedSession,
    highlightedGroupKey,
    navigate,
    highlightSession,
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node_modules/.bin/vitest run src/session/__tests__/useListNavigation.test.ts`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add web/src/session/useListNavigation.ts web/src/session/__tests__/useListNavigation.test.ts
git commit -m "feat(web): add useListNavigation shared cursor hook

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Header keybindings — `matchHeaderShortcut` + help section

**Files:**
- Modify: `web/src/lib/shortcuts.ts`
- Modify: `web/src/components/HelpDrawer.tsx` (add one section)
- Test: `web/src/lib/shortcuts.test.ts` (add cases)

**Interfaces:**
- Produces:
  ```ts
  type ListRowAction = … | { type: 'expand' } | { type: 'collapse' } | { type: 'toggle' }
  type ShortcutGroup = 'compose' | 'list-mode' | 'list-mode-header' | 'in-session'
  export function matchHeaderShortcut(e: KeyboardLike): ListRowAction | undefined
  ```
- Note: `matchListShortcut` is scoped to exclude the `list-mode-header` group so header keys never shadow session-row keys (e.g. `ArrowRight`).

- [ ] **Step 1: Write the failing test** (append a new `describe` to `web/src/lib/shortcuts.test.ts`)

```ts
import { matchHeaderShortcut } from './shortcuts'

describe('shortcuts registry — group headers', () => {
  it('maps header keys to expand/collapse/toggle/navigate/exit', () => {
    expect(matchHeaderShortcut(ev({ key: 'ArrowRight' }))).toEqual({ type: 'expand' })
    expect(matchHeaderShortcut(ev({ key: 'ArrowLeft' }))).toEqual({ type: 'collapse' })
    expect(matchHeaderShortcut(ev({ key: 'Enter' }))).toEqual({ type: 'toggle' })
    expect(matchHeaderShortcut(ev({ key: ' ' }))).toEqual({ type: 'toggle' })
    expect(matchHeaderShortcut(ev({ key: 'ArrowUp' }))).toEqual({ type: 'navigate', dir: 'up' })
    expect(matchHeaderShortcut(ev({ key: 'ArrowDown' }))).toEqual({ type: 'navigate', dir: 'down' })
    expect(matchHeaderShortcut(ev({ key: 'Escape' }))).toEqual({ type: 'exit' })
    expect(matchHeaderShortcut(ev({ key: 'Tab' }))).toEqual({ type: 'exit' })
  })

  it('header keys never leak into matchListShortcut (rows keep their meaning)', () => {
    expect(matchListShortcut(ev({ key: 'ArrowRight' }))).toEqual({ type: 'open' })
    expect(matchListShortcut(ev({ key: 'ArrowLeft' }))).toEqual({ type: 'exit' })
  })

  it('ignores header keys when a Ctrl/Alt/Meta modifier is held', () => {
    expect(matchHeaderShortcut(ev({ key: 'ArrowRight', metaKey: true }))).toBeUndefined()
  })

  it('exposes display entries for the list-mode-header group', () => {
    expect(shortcutsByGroup('list-mode-header').length).toBeGreaterThan(0)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node_modules/.bin/vitest run src/lib/shortcuts.test.ts`
Expected: FAIL — `matchHeaderShortcut` undefined.

- [ ] **Step 3: Write minimal implementation** in `web/src/lib/shortcuts.ts`

(a) Extend the action union:
```ts
export type ListRowAction =
  | { type: 'open' }
  | { type: 'continue' }
  | { type: 'rename' }
  | { type: 'archive' }
  | { type: 'permanent-delete' }
  | { type: 'navigate'; dir: 'up' | 'down' }
  | { type: 'exit' } // back to compose (focus the prompt)
  | { type: 'expand' } // group header: open the group
  | { type: 'collapse' } // group header: close the group
  | { type: 'toggle' } // group header: toggle open/closed
```

(b) Extend the group union:
```ts
export type ShortcutGroup = 'compose' | 'list-mode' | 'list-mode-header' | 'in-session'
```

(c) Add header entries to `SHORTCUTS` (place right after the `list.exit` entry):
```ts
  // ── List mode, group header focused — WIRED ──────────────────────────
  { id: 'header.expand', keys: '→', description: 'Expand the focused group',
    group: 'list-mode-header', action: { type: 'expand' }, match: plain((e) => e.key === 'ArrowRight') },
  { id: 'header.collapse', keys: '←', description: 'Collapse the focused group',
    group: 'list-mode-header', action: { type: 'collapse' }, match: plain((e) => e.key === 'ArrowLeft') },
  { id: 'header.toggle', keys: 'Enter / Space', description: 'Toggle the focused group',
    group: 'list-mode-header', action: { type: 'toggle' },
    match: plain((e) => e.key === 'Enter' || e.key === ' ') },
  { id: 'header.up', keys: '↑', description: 'Move to the previous row',
    group: 'list-mode-header', action: { type: 'navigate', dir: 'up' }, match: plain((e) => e.key === 'ArrowUp') },
  { id: 'header.down', keys: '↓', description: 'Move to the next row',
    group: 'list-mode-header', action: { type: 'navigate', dir: 'down' }, match: plain((e) => e.key === 'ArrowDown') },
  { id: 'header.exit', keys: 'Esc / Tab', description: 'Return to the prompt (compose)',
    group: 'list-mode-header', action: { type: 'exit' },
    match: plain((e) => e.key === 'Escape' || e.key === 'Tab') },
```

(d) Scope `matchListShortcut` so header entries can't shadow row keys, and add `matchHeaderShortcut`:
```ts
export function matchListShortcut(e: KeyboardLike): ListRowAction | undefined {
  return SHORTCUTS.find((s) => s.group !== 'list-mode-header' && s.match?.(e))?.action
}

export function matchHeaderShortcut(e: KeyboardLike): ListRowAction | undefined {
  return SHORTCUTS.find((s) => s.group === 'list-mode-header' && s.match?.(e))?.action
}
```

(e) In `web/src/components/HelpDrawer.tsx`, add a section after the existing `list-mode` line (around line 115):
```tsx
        <ShortcutSection title="Group headers" bindings={shortcutsByGroup('list-mode-header')} />
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node_modules/.bin/vitest run src/lib/shortcuts.test.ts`
Expected: PASS (existing + new)

- [ ] **Step 5: Commit**

```bash
git add web/src/lib/shortcuts.ts web/src/components/HelpDrawer.tsx web/src/lib/shortcuts.test.ts
git commit -m "feat(web): add group-header keybindings (matchHeaderShortcut) + help section

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: `GroupHeader` component (focusable, keyboard-driven)

**Files:**
- Create: `web/src/components/GroupHeader.tsx`
- Test: `web/src/components/__tests__/GroupHeader.test.tsx` (create)

**Interfaces:**
- Consumes: `matchHeaderShortcut` (Task 4), `cn`, lucide `ChevronRight`/`ChevronDown`.
- Produces:
  ```ts
  interface GroupHeaderProps {
    groupKey: string
    label: string
    count: number
    collapsed: boolean
    selected?: boolean
    listMode?: boolean
    onToggle: (key: string) => void
    onExpand?: (key: string) => void
    onCollapse?: (key: string) => void
    onNavigate?: (dir: 'up' | 'down') => void
    onExit?: () => void
  }
  export function GroupHeader(props: GroupHeaderProps): JSX.Element
  ```
- The header button has `id={`group-header-${groupKey}`}`. In list mode, when `selected`, it takes real DOM focus (mirrors `SessionCard` row focus). Mouse click still toggles via `onToggle`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/__tests__/GroupHeader.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { GroupHeader } from '../GroupHeader'

function setup(over = {}) {
  const props = {
    groupKey: 'busy', label: 'Working', count: 2, collapsed: false,
    selected: true, listMode: true,
    onToggle: vi.fn(), onExpand: vi.fn(), onCollapse: vi.fn(),
    onNavigate: vi.fn(), onExit: vi.fn(), ...over,
  }
  render(<GroupHeader {...props} />)
  return props
}

describe('GroupHeader', () => {
  it('renders label, count and an expanded chevron; has a stable id', () => {
    setup()
    const btn = screen.getByRole('button', { name: /Working/ })
    expect(btn).toHaveAttribute('id', 'group-header-busy')
    expect(btn).toHaveAttribute('aria-expanded', 'true')
  })

  it('→ expands, ← collapses, Enter toggles (list mode)', async () => {
    const u = userEvent.setup()
    const p = setup({ collapsed: true })
    const btn = screen.getByRole('button', { name: /Working/ })
    btn.focus()
    await u.keyboard('{ArrowRight}')
    expect(p.onExpand).toHaveBeenCalledWith('busy')
    await u.keyboard('{ArrowLeft}')
    expect(p.onCollapse).toHaveBeenCalledWith('busy')
    await u.keyboard('{Enter}')
    expect(p.onToggle).toHaveBeenCalledWith('busy')
  })

  it('↑/↓ navigate and Esc exits', async () => {
    const u = userEvent.setup()
    const p = setup()
    screen.getByRole('button', { name: /Working/ }).focus()
    await u.keyboard('{ArrowDown}')
    expect(p.onNavigate).toHaveBeenCalledWith('down')
    await u.keyboard('{Escape}')
    expect(p.onExit).toHaveBeenCalled()
  })

  it('mouse click toggles', async () => {
    const u = userEvent.setup()
    const p = setup({ listMode: false, selected: false })
    await u.click(screen.getByRole('button', { name: /Working/ }))
    expect(p.onToggle).toHaveBeenCalledWith('busy')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node_modules/.bin/vitest run src/components/__tests__/GroupHeader.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```tsx
// web/src/components/GroupHeader.tsx
import { useEffect, useRef } from 'react'
import { ChevronRight, ChevronDown } from 'lucide-react'
import { matchHeaderShortcut } from '@/lib/shortcuts'
import { cn } from '@/lib/utils'

interface GroupHeaderProps {
  groupKey: string
  label: string
  count: number
  collapsed: boolean
  /** Keyboard cursor is on this header. */
  selected?: boolean
  /** When true, the selected header takes real DOM focus so single keys land here. */
  listMode?: boolean
  onToggle: (key: string) => void
  onExpand?: (key: string) => void
  onCollapse?: (key: string) => void
  onNavigate?: (dir: 'up' | 'down') => void
  onExit?: () => void
}

export function GroupHeader({
  groupKey, label, count, collapsed, selected = false, listMode = false,
  onToggle, onExpand, onCollapse, onNavigate, onExit,
}: GroupHeaderProps) {
  const ref = useRef<HTMLButtonElement>(null)
  const panelId = `group-panel-${groupKey}`

  // In list-focus mode, give the highlighted header REAL keyboard focus so the
  // single-key shortcuts land here (mirrors SessionCard row focus).
  useEffect(() => {
    if (listMode && selected) ref.current?.focus()
  }, [listMode, selected])

  function handleKeyDown(e: React.KeyboardEvent) {
    const action = matchHeaderShortcut(e)
    if (!action) return
    e.preventDefault()
    switch (action.type) {
      case 'expand': onExpand?.(groupKey); break
      case 'collapse': onCollapse?.(groupKey); break
      case 'toggle': onToggle(groupKey); break
      case 'navigate': onNavigate?.(action.dir); break
      case 'exit': onExit?.(); break
    }
  }

  return (
    <button
      ref={ref}
      type="button"
      id={`group-header-${groupKey}`}
      tabIndex={selected ? 0 : -1}
      data-selected={selected}
      onClick={() => onToggle(groupKey)}
      onKeyDown={handleKeyDown}
      aria-expanded={!collapsed}
      aria-controls={collapsed ? undefined : panelId}
      className={cn(
        'sticky top-0 z-10 flex w-full items-center gap-2 px-1 py-1',
        'bg-gray-50/95 dark:bg-gray-900/95 backdrop-blur',
        'text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400',
        'hover:text-gray-700 dark:hover:text-gray-200',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 rounded',
        selected && 'ring-2 ring-blue-500 text-gray-700 dark:text-gray-200',
      )}
    >
      {collapsed ? <ChevronRight size={14} aria-hidden /> : <ChevronDown size={14} aria-hidden />}
      <span>{label}</span>
      <span className="tabular-nums text-gray-400 dark:text-gray-500">{count}</span>
    </button>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node_modules/.bin/vitest run src/components/__tests__/GroupHeader.test.tsx`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add web/src/components/GroupHeader.tsx web/src/components/__tests__/GroupHeader.test.tsx
git commit -m "feat(web): add focusable GroupHeader with keyboard collapse/expand

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Wire `GroupHeader` into `SessionList` (header cursor + activedescendant)

**Files:**
- Modify: `web/src/components/SessionList.tsx`
- Test: `web/src/components/__tests__/SessionList.test.tsx` (add a grouped-nav case)

**Interfaces:**
- Consumes: `GroupHeader` (Task 5).
- Produces (added to `SessionListProps`):
  ```ts
  selectedGroupKey?: string | null
  onExpand?: (key: string) => void
  onCollapse?: (key: string) => void
  ```
  (`onToggleCollapse`, `onNavigate`, `onExit`, `selectedId` already exist.)

- [ ] **Step 1: Write the failing test** (add to `web/src/components/__tests__/SessionList.test.tsx`; reuse its existing session/account fixtures and `groups` shape — match the file's existing helpers)

```tsx
  it('renders a highlighted group header and routes its keys', async () => {
    const u = userEvent.setup()
    const onExpand = vi.fn()
    const groups = [
      { key: 'busy', label: 'Working', count: 1, sessions: [mkSession('s1')] },
    ]
    render(
      <SessionList
        sessions={groups.flatMap((g) => g.sessions)}
        groups={groups}
        accounts={[]}
        listMode
        collapsedKeys={new Set(['busy'])}
        selectedGroupKey="busy"
        onToggleCollapse={vi.fn()}
        onExpand={onExpand}
        onCollapse={vi.fn()}
        onNavigate={vi.fn()}
        onExit={vi.fn()}
        onNew={vi.fn()} onOpen={vi.fn()} onRename={vi.fn()}
        onClose={vi.fn()} onDelete={vi.fn()} onQuickResume={vi.fn()}
      />,
    )
    const header = screen.getByRole('button', { name: /Working/ })
    expect(header).toHaveAttribute('id', 'group-header-busy')
    header.focus()
    await u.keyboard('{ArrowRight}')
    expect(onExpand).toHaveBeenCalledWith('busy')
  })
```
(If `mkSession` does not exist in the file, define a minimal local factory matching the file's other tests.)

- [ ] **Step 2: Run test to verify it fails**

Run: `node_modules/.bin/vitest run src/components/__tests__/SessionList.test.tsx`
Expected: FAIL — header not focusable / props not wired.

- [ ] **Step 3: Write minimal implementation** in `web/src/components/SessionList.tsx`

(a) Replace the `lucide-react` import line (chevrons now live in `GroupHeader`) and import the component:
```tsx
import { cn } from '@/lib/utils'
import { SessionCard } from './SessionCard'
import { GroupHeader } from './GroupHeader'
import { EmptyState } from './EmptyState'
```
(Remove `import { ChevronRight, ChevronDown } from 'lucide-react'` — it is no longer used here.)

(b) Add the new props to the interface and the destructure:
```ts
  collapsedKeys?: Set<string>
  onToggleCollapse?: (key: string) => void
  selectedGroupKey?: string | null
  onExpand?: (key: string) => void
  onCollapse?: (key: string) => void
  listMode?: boolean
  onNavigate?: (dir: 'up' | 'down') => void
  onExit?: () => void
```
```ts
  collapsedKeys,
  onToggleCollapse,
  selectedGroupKey,
  onExpand,
  onCollapse,
  listMode,
  onNavigate,
  onExit,
```

(c) Update the container `aria-activedescendant` to cover a highlighted header:
```tsx
    <div
      id="session-listbox"
      aria-label="Sessions"
      aria-activedescendant={
        selectedId ? `row-${selectedId}` : selectedGroupKey ? `group-header-${selectedGroupKey}` : undefined
      }
      className="space-y-2"
    >
```

(d) Replace the inline header `<button>` (the whole `group.label !== null && ( … )` block) with `GroupHeader`:
```tsx
                {group.label !== null && (
                  <GroupHeader
                    groupKey={group.key}
                    label={group.label}
                    count={group.count}
                    collapsed={collapsed}
                    selected={group.key === selectedGroupKey}
                    listMode={listMode}
                    onToggle={(k) => onToggleCollapse?.(k)}
                    onExpand={onExpand}
                    onCollapse={onCollapse}
                    onNavigate={onNavigate}
                    onExit={onExit}
                  />
                )}
```
(Leave the `{!collapsed && (<div id={panelId}>…</div>)}` block unchanged — note `panelId` stays defined in the `.map`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `node_modules/.bin/vitest run src/components/__tests__/SessionList.test.tsx`
Expected: PASS (existing + new)

- [ ] **Step 5: Commit**

```bash
git add web/src/components/SessionList.tsx web/src/components/__tests__/SessionList.test.tsx
git commit -m "feat(web): render headers via GroupHeader; header cursor + activedescendant

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: `DispatchBar` — header branch for empty-prompt arrows / Enter

**Files:**
- Modify: `web/src/components/DispatchBar.tsx`
- Test: `web/src/components/__tests__/DispatchBar.test.tsx` (add cases)

**Interfaces:**
- Produces (added to `DispatchBarProps`):
  ```ts
  onExpandHighlighted?: () => void
  onCollapseHighlighted?: () => void
  onToggleHighlighted?: () => void
  activeDescendantIsHeader?: boolean
  ```
- Behavior (empty prompt only; non-empty prompt is unchanged so the caret moves / draft is safe):
  - `→`: header → `onExpandHighlighted`, else `onOpenHighlighted` (today).
  - `←`: header → `onCollapseHighlighted`; non-header → unchanged (no-op).
  - `Enter`: header → `onToggleHighlighted`; non-header → `dispatchNow()` (today).
  - `Space` is NOT intercepted in compose (must type a space) — toggle-on-space is list-mode only.

- [ ] **Step 1: Write the failing test** (append to `web/src/components/__tests__/DispatchBar.test.tsx`, matching its existing render helper / default props)

```tsx
  it('empty prompt + header highlighted: → expands, ← collapses, Enter toggles', async () => {
    const u = userEvent.setup()
    const onExpandHighlighted = vi.fn()
    const onCollapseHighlighted = vi.fn()
    const onToggleHighlighted = vi.fn()
    const onOpenHighlighted = vi.fn()
    renderDispatchBar({
      activeDescendantIsHeader: true,
      onExpandHighlighted, onCollapseHighlighted, onToggleHighlighted, onOpenHighlighted,
    })
    const ta = screen.getByLabelText('New session prompt')
    ta.focus()
    await u.keyboard('{ArrowRight}')
    expect(onExpandHighlighted).toHaveBeenCalled()
    expect(onOpenHighlighted).not.toHaveBeenCalled()
    await u.keyboard('{ArrowLeft}')
    expect(onCollapseHighlighted).toHaveBeenCalled()
    await u.keyboard('{Enter}')
    expect(onToggleHighlighted).toHaveBeenCalled()
  })

  it('empty prompt + session highlighted: → still opens (unchanged)', async () => {
    const u = userEvent.setup()
    const onOpenHighlighted = vi.fn()
    renderDispatchBar({ activeDescendantIsHeader: false, onOpenHighlighted })
    const ta = screen.getByLabelText('New session prompt')
    ta.focus()
    await u.keyboard('{ArrowRight}')
    expect(onOpenHighlighted).toHaveBeenCalled()
  })
```
(If the file lacks a `renderDispatchBar` helper, render `<DispatchBar {...defaults} {...over} />` inline with required no-op props from `DispatchBarProps`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `node_modules/.bin/vitest run src/components/__tests__/DispatchBar.test.tsx`
Expected: FAIL — new props not handled.

- [ ] **Step 3: Write minimal implementation** in `web/src/components/DispatchBar.tsx`

(a) Add to `DispatchBarProps`:
```ts
  /** Empty prompt + header highlighted: → expands it. */
  onExpandHighlighted?: () => void
  /** Empty prompt + header highlighted: ← collapses it. */
  onCollapseHighlighted?: () => void
  /** Empty prompt + header highlighted: Enter toggles it. */
  onToggleHighlighted?: () => void
  /** True when the active descendant is a group header (not a session row). */
  activeDescendantIsHeader?: boolean
```
(b) Destructure them in the component signature (with the other props).
(c) In `handleKeyDown`, update the Enter branch:
```ts
    // Enter (no Shift) → toggle a highlighted header (empty prompt), else dispatch/open.
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      if (empty && activeDescendantIsHeader) { onToggleHighlighted?.(); return }
      dispatchNow()
      return
    }
```
(d) Replace the `ArrowRight` branch and add an `ArrowLeft` branch (place both where the current `ArrowRight` block is):
```ts
    // ArrowRight (empty prompt) → expand a highlighted header, else open the highlighted session.
    if (empty && e.key === 'ArrowRight') {
      e.preventDefault()
      if (activeDescendantIsHeader) onExpandHighlighted?.()
      else onOpenHighlighted()
      return
    }

    // ArrowLeft (empty prompt) → collapse a highlighted header. (No-op otherwise.)
    if (empty && e.key === 'ArrowLeft' && activeDescendantIsHeader) {
      e.preventDefault()
      onCollapseHighlighted?.()
      return
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node_modules/.bin/vitest run src/components/__tests__/DispatchBar.test.tsx`
Expected: PASS (existing + new)

- [ ] **Step 5: Commit**

```bash
git add web/src/components/DispatchBar.tsx web/src/components/__tests__/DispatchBar.test.tsx
git commit -m "feat(web): DispatchBar collapses/expands highlighted header on empty-prompt arrows

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: Wire `DesktopShell` to `useListNavigation` + header props

**Files:**
- Modify: `web/src/components/DesktopShell.tsx`

**Interfaces:**
- Consumes: `useListNavigation` (Task 3); `navItems`/`expandGroup`/`collapseGroup`/`toggleCollapse` from `useSessionOrdering` (Task 2); `SessionList`/`DispatchBar` new props (Tasks 6, 7).

- [ ] **Step 1: Apply the wiring changes**

(a) Pull the new ordering fields (line ~115):
```ts
  const { groupBy, sortBy, setGroupBy, setSortBy, groups, orderedSessions, navItems, collapsedKeys, toggleCollapse, expandGroup, collapseGroup } =
    useSessionOrdering(sessions, sessionSearch.matchedIds)
```

(b) Replace the local cursor state + clamp (lines ~135-137 and ~148-150) with the hook. Add the import at top:
```ts
import { useListNavigation } from '@/session/useListNavigation'
```
Replace:
```ts
  const [highlightedIndex, setHighlightedIndex] = useState(-1)
  const highlighted = highlightedIndex >= 0 ? orderedSessions[highlightedIndex] : undefined
```
and the later clamp block:
```ts
  const clampedIndex = orderedSessions.length === 0 ? -1 : Math.min(highlightedIndex, orderedSessions.length - 1)
  if (clampedIndex !== highlightedIndex) setHighlightedIndex(clampedIndex)
```
with (put the hook call where the state was; delete the clamp block):
```ts
  const {
    highlightedIndex, setHighlightedIndex,
    highlightedSession, highlightedGroupKey, navigate, highlightSession,
  } = useListNavigation(navItems)
```

(c) `detachToList` (lines ~167-175): replace the manual index lookup with `highlightSession`:
```ts
  const detachToList = useCallback(() => {
    const id = selection.type === 'session' ? selection.id : null
    setSelection({ type: 'new' })
    if (id) highlightSession(id)
    setDispatchFocusNonce((n) => n + 1) // return focus to DispatchBar for ↑/↓ nav
  }, [selection, highlightSession])
```

(d) `handleNavigate` (lines ~255-261): delete it and use `navigate` everywhere it was passed. (Search for `handleNavigate` usages and replace with `navigate`.)

(e) `handleOpenHighlighted` (line ~263): use the session:
```ts
  function handleOpenHighlighted() {
    if (highlightedSession) focusSession(highlightedSession.id)
  }
```

(f) `handleEnterList` (line ~267-270): unchanged logic but uses `setHighlightedIndex`:
```ts
  function handleEnterList() {
    setListMode(true)
    if (highlightedIndex < 0) setHighlightedIndex(0)
  }
```

(g) `handleJump` (lines ~275-278): highlight by session then open:
```ts
  function handleJump(index: number) {
    const s = orderedSessions[index]
    if (s) { highlightSession(s.id); focusSession(s.id) }
  }
```

(h) Add header action handlers (near the other DispatchBar handlers):
```ts
  function handleExpandHighlighted() { if (highlightedGroupKey) expandGroup(highlightedGroupKey) }
  function handleCollapseHighlighted() { if (highlightedGroupKey) collapseGroup(highlightedGroupKey) }
  function handleToggleHighlighted() { if (highlightedGroupKey) toggleCollapse(highlightedGroupKey) }
```

(i) Update the `<SessionList>` props (lines ~380-402): replace `selectedId={selectedSessionId ?? highlighted?.id}` and add the header props:
```tsx
          selectedId={selectedSessionId ?? highlightedSession?.id}
          selectedGroupKey={highlightedGroupKey ?? null}
          listMode={listMode}
          onNavigate={navigate}
          onExit={handleExitList}
          collapsedKeys={collapsedKeys}
          onToggleCollapse={toggleCollapse}
          onExpand={expandGroup}
          onCollapse={collapseGroup}
```
(Keep the other existing props; ensure `onExit` matches the existing exit handler name used here — if it currently passes `onExit={handleExitList}` keep it.)

(j) Update the `<DispatchBar>` props (lines ~426-437):
```tsx
      onNavigate={navigate}
      onOpenHighlighted={handleOpenHighlighted}
      onExpandHighlighted={handleExpandHighlighted}
      onCollapseHighlighted={handleCollapseHighlighted}
      onToggleHighlighted={handleToggleHighlighted}
      activeDescendantId={
        highlightedSession ? `row-${highlightedSession.id}`
        : highlightedGroupKey ? `group-header-${highlightedGroupKey}`
        : undefined
      }
      activeDescendantIsHeader={!!highlightedGroupKey}
```

(k) Remove the now-unused `useState` import only if nothing else uses it (it is used elsewhere — leave it).

- [ ] **Step 2: Type-check + lint + run the relevant suites**

Run:
```bash
node_modules/.bin/tsc -b --force
node_modules/.bin/eslint src/components/DesktopShell.tsx
node_modules/.bin/vitest run src/components
```
Expected: tsc exit 0, eslint clean, component tests pass. Fix any type/prop mismatches surfaced (e.g. a missed `highlighted` reference → `highlightedSession`).

- [ ] **Step 3: Commit**

```bash
git add web/src/components/DesktopShell.tsx
git commit -m "feat(web): wire DesktopShell cursor to useListNavigation + header keys

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 9: Wire `Dashboard` to `useListNavigation` + header props

**Files:**
- Modify: `web/src/components/Dashboard.tsx`

**Interfaces:**
- Same as Task 8, applied to the `<1024px` layout.

- [ ] **Step 1: Apply the wiring changes**

(a) Add import:
```ts
import { useListNavigation } from '@/session/useListNavigation'
```

(b) Pull new ordering fields (line ~1187):
```ts
  const { groupBy, sortBy, setGroupBy, setSortBy, groups, orderedSessions, navItems, collapsedKeys, toggleCollapse, expandGroup, collapseGroup } =
    useSessionOrdering(sessions, sessionSearch.matchedIds)
```

(c) Replace the local cursor state (lines ~1239-1241) and the clamp block (lines ~1260-1266) with the hook:
```ts
  const {
    highlightedIndex, setHighlightedIndex,
    highlightedSession, highlightedGroupKey, navigate, highlightSession,
  } = useListNavigation(navItems)
  const selectedId = highlightedSession?.id ?? null
```
Delete the old `const [highlightedIndex, setHighlightedIndex] = useState(-1)`, `selectedSession`, `selectedId` lines and the entire clamp block (`const clampedIndex = … if (clampedIndex !== highlightedIndex) { … }`).

(d) `handleNavigate` (lines ~1375-1381): delete; replace usages with `navigate`. The `useGlobalListNav` call (line ~1383) becomes:
```ts
  useGlobalListNav({ enabled: activeTab === 'sessions', onNavigate: navigate })
```

(e) `handleOpenHighlighted`: use `highlightedSession`:
```ts
  function handleOpenHighlighted() {
    if (highlightedSession) /* existing open logic, e.g. */ focusSession(highlightedSession.id)
  }
```
(Match the existing open call used in this file — keep its current body, only swap the source from `selectedSession`/`orderedSessions[highlightedIndex]` to `highlightedSession`.)

(f) `handleEnterList` (line ~1249): keep `if (highlightedIndex < 0) setHighlightedIndex(0)`.

(g) `handleJump` (line ~1256): `const s = orderedSessions[index]; if (s) { highlightSession(s.id); /* existing open */ }`.

(h) Add header handlers:
```ts
  function handleExpandHighlighted() { if (highlightedGroupKey) expandGroup(highlightedGroupKey) }
  function handleCollapseHighlighted() { if (highlightedGroupKey) collapseGroup(highlightedGroupKey) }
  function handleToggleHighlighted() { if (highlightedGroupKey) toggleCollapse(highlightedGroupKey) }
```

(i) `<SessionList>` props (lines ~1668-1686): add and adjust:
```tsx
            selectedId={selectedId}
            selectedGroupKey={highlightedGroupKey ?? null}
            listMode={listMode}
            onNavigate={navigate}
            collapsedKeys={collapsedKeys}
            onToggleCollapse={toggleCollapse}
            onExpand={expandGroup}
            onCollapse={collapseGroup}
```
(Keep the existing `onExit` if present.)

(j) `<DispatchBar>` props (lines ~1691-1702):
```tsx
            onNavigate={navigate}
            onOpenHighlighted={handleOpenHighlighted}
            onExpandHighlighted={handleExpandHighlighted}
            onCollapseHighlighted={handleCollapseHighlighted}
            onToggleHighlighted={handleToggleHighlighted}
            activeDescendantId={
              highlightedSession ? `row-${highlightedSession.id}`
              : highlightedGroupKey ? `group-header-${highlightedGroupKey}`
              : undefined
            }
            activeDescendantIsHeader={!!highlightedGroupKey}
```

- [ ] **Step 2: Type-check + lint + run suites**

Run:
```bash
node_modules/.bin/tsc -b --force
node_modules/.bin/eslint src/components/Dashboard.tsx
node_modules/.bin/vitest run src/components
```
Expected: tsc exit 0, eslint clean, tests pass. The memory note "Dashboard hooks partial mocks" warns adding a hook can break ~9 Dashboard test factory mocks — if a Dashboard test fails on a missing mock, update that test's mock setup (do NOT change behavior to satisfy a stale mock).

- [ ] **Step 3: Commit**

```bash
git add web/src/components/Dashboard.tsx
git commit -m "feat(web): wire Dashboard cursor to useListNavigation + header keys

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 10: Full-suite verification + spec sync

**Files:**
- Modify (docs): `docs/superpowers/specs/2026-06-24-group-header-keyboard-nav-design.md` (flip Status to Implemented) — optional.

- [ ] **Step 1: Run the whole web gate**

Run (from `web/`):
```bash
node_modules/.bin/vitest run
node_modules/.bin/tsc -b --force
node_modules/.bin/eslint .
```
Expected: all green — vitest all files pass (≥1124 + new tests), tsc exit 0, eslint exit 0.

- [ ] **Step 2: Manual smoke (optional, if a dev server is available)** — group by status/repo, Tab into list mode, `↓` onto a header, `←`/`→` collapse/expand, `Enter` toggle, `↑`/`↓` move off; confirm session-row `→`/`←` still open/exit; confirm ungrouped view unchanged.

- [ ] **Step 3: Commit any doc sync + push / open PR** per the project completion workflow:
```bash
git push -u origin feat/group-header-keyboard-nav
gh pr create --title "feat(web): keyboard collapse/expand of session group headers" --body "<summary + Fixes/Refs + Claude Code trailer>"
gh pr merge <#> --squash --auto
```

---

## Self-Review

**Spec coverage:**
- Nav model (`NavItem`, `buildNavItems`, ungrouped parity) → Task 1. ✓
- `navItems` + collapse helpers in `useSessionOrdering` → Task 2. ✓
- `useListNavigation` (cursor, clamp, derive header/session, highlightSession) → Task 3. ✓
- Header keybindings in `shortcuts.ts` + help panel → Task 4. ✓
- Header rendering / focus / keydown (`GroupHeader`) → Task 5; SessionList wiring + activedescendant → Task 6. ✓
- Compose-mode empty-prompt header arrows/Enter (`DispatchBar`) → Task 7. ✓
- Both layouts consume the shared hook → Tasks 8 (DesktopShell) + 9 (Dashboard). ✓
- Testing (unit + component, both layouts) → per-task tests + Task 10 full gate. ✓
- Decisions: simple collapse/expand (Task 4 actions), both modes (Tasks 6+7), Enter toggles (Tasks 4/5/7), session rows unchanged (Task 4 scoping + Task 7 guards). ✓

**Placeholder scan:** No TBD/TODO; every code step shows real code; wiring tasks (8,9) reference exact line anchors and exact replacement code.

**Type consistency:** `NavItem`, `buildNavItems`, `useListNavigation` (`highlightedSession`/`highlightedGroupKey`/`navigate`/`highlightSession`), `useSessionOrdering` (`navItems`/`expandGroup`/`collapseGroup`/`toggleCollapse`), `matchHeaderShortcut`, `GroupHeader` props, `SessionList` props (`selectedGroupKey`/`onExpand`/`onCollapse`), `DispatchBar` props (`onExpandHighlighted`/`onCollapseHighlighted`/`onToggleHighlighted`/`activeDescendantIsHeader`) are used identically across producing and consuming tasks.
