# Collapsible Session Groups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users expand/collapse each group in the session list (status / repo grouping), with collapsed state persisted to localStorage.

**Architecture:** A new `lib/groupCollapse.ts` persists a `Set<string>` of *collapsed* group keys (mirroring `lib/sessionOrder.ts`). `useSessionOrdering` owns that state, exposes `collapsedKeys` + `toggleCollapse`, and excludes collapsed groups' sessions from `orderedSessions` (so `DispatchBar` keyboard nav never lands on a hidden row). `SessionList` turns each group header into an accessible disclosure button that hides the group's cards when collapsed. Both hook consumers (DesktopShell, Dashboard) thread the two new props through.

**Tech Stack:** React + TypeScript + Vite, Tailwind, lucide-react icons, Vitest + @testing-library/react, ESLint.

## Global Constraints

- Store *collapsed* keys (empty set = everything expanded). New/unknown groups default expanded.
- Group identity is the existing `group.key` from `orderSessions` (status → tier name; repo → repo label). A single shared `Set<string>` works for both grouping modes; stale keys are harmless.
- All localStorage access is try/catch-wrapped, best-effort; failure degrades to "nothing collapsed".
- Never mutate `Set` state in place — always create a new `Set` so React re-renders.
- Collapse applies only when grouping is active. `None` mode (`label === null`, single `all` group) is unchanged.
- Real type gate is `npx tsc -b --force` (the `typecheck`/`tsc --noEmit` script checks nothing here). Lint gate: `npm run lint`. Tests: `npx vitest run <file>` for a single file, `npm test` for all.
- All `web/` commands run from the `web/` directory. `node_modules` may need to be symlinked from the deploy worktree (see CLAUDE.md / project memory) — use the local `.bin`, not `npx` fetching from network.

---

### Task 1: `groupCollapse.ts` persistence lib

**Files:**
- Create: `web/src/lib/groupCollapse.ts`
- Test: `web/src/lib/__tests__/groupCollapse.test.ts`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `loadGroupCollapse(): Set<string>`
  - `saveGroupCollapse(keys: Set<string>): void`
  - Storage key: `ccswitch.groupCollapse`; on-disk shape: JSON `string[]`.

- [ ] **Step 1: Write the failing test**

Create `web/src/lib/__tests__/groupCollapse.test.ts`:

```typescript
import { describe, it, expect, beforeEach } from 'vitest'
import { loadGroupCollapse, saveGroupCollapse } from '../groupCollapse'

describe('groupCollapse', () => {
  beforeEach(() => window.localStorage.clear())

  it('defaults to an empty set when nothing is stored', () => {
    expect(loadGroupCollapse()).toEqual(new Set())
  })

  it('round-trips a set of keys', () => {
    saveGroupCollapse(new Set(['busy', 'Scratch']))
    expect(loadGroupCollapse()).toEqual(new Set(['busy', 'Scratch']))
  })

  it('serializes to a JSON string array', () => {
    saveGroupCollapse(new Set(['busy']))
    expect(window.localStorage.getItem('ccswitch.groupCollapse')).toBe('["busy"]')
  })

  it('falls back to an empty set on malformed JSON', () => {
    window.localStorage.setItem('ccswitch.groupCollapse', '{not json')
    expect(loadGroupCollapse()).toEqual(new Set())
  })

  it('falls back to an empty set when the payload is not an array', () => {
    window.localStorage.setItem('ccswitch.groupCollapse', '{"a":1}')
    expect(loadGroupCollapse()).toEqual(new Set())
  })

  it('ignores non-string array entries', () => {
    window.localStorage.setItem('ccswitch.groupCollapse', '["busy",1,null]')
    expect(loadGroupCollapse()).toEqual(new Set(['busy']))
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `web/`): `npx vitest run src/lib/__tests__/groupCollapse.test.ts`
Expected: FAIL — cannot resolve `../groupCollapse`.

- [ ] **Step 3: Write minimal implementation**

Create `web/src/lib/groupCollapse.ts`:

```typescript
/**
 * Persists which session-list groups are collapsed. Mirrors sessionOrder.ts:
 * all access is try/catch-wrapped so a blocked/unavailable localStorage degrades
 * to "nothing collapsed" instead of crashing. We store the set of COLLAPSED group
 * keys (so an empty/absent value means everything is expanded, and any newly
 * appearing group defaults to expanded). Keys are the stable group.key produced by
 * orderSessions (status tier name or repo label).
 */
const KEY = 'ccswitch.groupCollapse'

export function loadGroupCollapse(): Set<string> {
  try {
    const raw = window.localStorage.getItem(KEY)
    if (!raw) return new Set()
    const parsed = JSON.parse(raw) as unknown
    if (!Array.isArray(parsed)) return new Set()
    return new Set(parsed.filter((k): k is string => typeof k === 'string'))
  } catch {
    return new Set()
  }
}

export function saveGroupCollapse(keys: Set<string>): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify([...keys]))
  } catch {
    /* ignore — persistence is best-effort */
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run (from `web/`): `npx vitest run src/lib/__tests__/groupCollapse.test.ts`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add web/src/lib/groupCollapse.ts web/src/lib/__tests__/groupCollapse.test.ts
git commit -m "feat(web): persist collapsed session-group keys to localStorage"
```

---

### Task 2: collapse state in `useSessionOrdering`

**Files:**
- Modify: `web/src/session/useSessionOrdering.ts`
- Test: `web/src/session/__tests__/useSessionOrdering.test.ts`

**Interfaces:**
- Consumes: `loadGroupCollapse`, `saveGroupCollapse` from Task 1.
- Produces (added to the `SessionOrdering` interface):
  - `collapsedKeys: Set<string>`
  - `toggleCollapse: (key: string) => void`
  - `orderedSessions` now excludes sessions whose group key is in `collapsedKeys`.

- [ ] **Step 1: Write the failing test**

Append these tests inside the existing top-level `describe` in
`web/src/session/__tests__/useSessionOrdering.test.ts`. (The file already imports
`renderHook`, `act` from `@testing-library/react`, `useSessionOrdering`, and defines
the `mk(id, over)` session factory — reuse them. Add `beforeEach(() => window.localStorage.clear())`
if the file does not already clear storage between tests.)

```typescript
it('toggleCollapse adds then removes a key, returning new sets', () => {
  const { result } = renderHook(() => useSessionOrdering([mk('s1')]))
  expect(result.current.collapsedKeys.has('busy')).toBe(false)
  act(() => result.current.toggleCollapse('busy'))
  expect(result.current.collapsedKeys.has('busy')).toBe(true)
  act(() => result.current.toggleCollapse('busy'))
  expect(result.current.collapsedKeys.has('busy')).toBe(false)
})

it('orderedSessions excludes sessions in a collapsed group (status mode)', () => {
  // s1 idle -> tier "your-turn"; s2 in_progress -> tier "busy"
  const { result } = renderHook(() =>
    useSessionOrdering([mk('s1', { status: 'idle' }), mk('s2', { status: 'in_progress' })]),
  )
  act(() => result.current.setGroupBy('status'))
  expect(result.current.orderedSessions.map((s) => s.id).sort()).toEqual(['s1', 's2'])
  act(() => result.current.toggleCollapse('busy'))
  expect(result.current.orderedSessions.map((s) => s.id)).toEqual(['s1'])
})

it('persists collapsed keys across a remount', () => {
  const first = renderHook(() => useSessionOrdering([mk('s1')]))
  act(() => first.result.current.toggleCollapse('busy'))
  first.unmount()
  const second = renderHook(() => useSessionOrdering([mk('s1')]))
  expect(second.result.current.collapsedKeys.has('busy')).toBe(true)
})
```

> Note: confirm `'busy'` is the tier key for `in_progress` and `'your-turn'` for `idle`
> by checking `web/src/session/statusTier.ts` and `ordering.ts` (Task uses them verbatim
> from there). If the factory's default `status` is not `idle`, pass `status` explicitly
> as shown.

- [ ] **Step 2: Run test to verify it fails**

Run (from `web/`): `npx vitest run src/session/__tests__/useSessionOrdering.test.ts`
Expected: FAIL — `result.current.collapsedKeys` / `toggleCollapse` is undefined.

- [ ] **Step 3: Write minimal implementation**

Edit `web/src/session/useSessionOrdering.ts`:

a) Update imports (line 4):

```typescript
import { loadSessionOrder, saveSessionOrder } from '@/lib/sessionOrder'
import { loadGroupCollapse, saveGroupCollapse } from '@/lib/groupCollapse'
```

b) Extend the `SessionOrdering` interface (after `orderedSessions: Session[]`):

```typescript
  collapsedKeys: Set<string>
  toggleCollapse: (key: string) => void
```

c) Add state + persistence + toggle (after the existing `groupBy`/`sortBy` state and effect):

```typescript
  const [collapsedKeys, setCollapsedKeys] = useState<Set<string>>(loadGroupCollapse)

  useEffect(() => {
    saveGroupCollapse(collapsedKeys)
  }, [collapsedKeys])

  const toggleCollapse = useCallback((key: string) => {
    setCollapsedKeys((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])
```

Add `useCallback` to the React import on line 1:

```typescript
import { useCallback, useEffect, useMemo, useState } from 'react'
```

d) Change `orderedSessions` to skip collapsed groups (replace the existing line 41):

```typescript
  const orderedSessions = useMemo(
    () => groups.filter((g) => !collapsedKeys.has(g.key)).flatMap((g) => g.sessions),
    [groups, collapsedKeys],
  )
```

e) Add the two new fields to the returned object:

```typescript
  return { groupBy, sortBy, setGroupBy, setSortBy, groups, orderedSessions, collapsedKeys, toggleCollapse }
```

- [ ] **Step 4: Run test to verify it passes**

Run (from `web/`): `npx vitest run src/session/__tests__/useSessionOrdering.test.ts`
Expected: PASS (existing tests + 3 new).

- [ ] **Step 5: Commit**

```bash
git add web/src/session/useSessionOrdering.ts web/src/session/__tests__/useSessionOrdering.test.ts
git commit -m "feat(web): track collapsed group state in useSessionOrdering"
```

---

### Task 3: disclosure button in `SessionList` + thread through consumers

**Files:**
- Modify: `web/src/components/SessionList.tsx`
- Modify: `web/src/components/DesktopShell.tsx` (hook destructure ~line 106; `<SessionList>` ~line 391)
- Modify: `web/src/components/Dashboard.tsx` (hook destructure ~line 1237; `<SessionList>` ~line 1757)
- Test: `web/src/components/__tests__/SessionList.test.tsx`

**Interfaces:**
- Consumes: `collapsedKeys: Set<string>`, `toggleCollapse` from Task 2.
- Produces: `SessionList` accepts optional `collapsedKeys?: Set<string>` and
  `onToggleCollapse?: (key: string) => void`. When omitted, rendering is unchanged
  (all groups expanded, headers stay non-interactive presentation as today is NOT
  required — see step 3: headers always become buttons when a label exists, and the
  toggle is a no-op if `onToggleCollapse` is undefined).

- [ ] **Step 1: Write the failing test**

Add to `web/src/components/__tests__/SessionList.test.tsx`. The file already has a
`groups` test (~line 285) and the `wrapper` QueryClient helper — reuse the existing
`SESSIONS`/`ACCOUNTS` fixtures and base props. Use two status groups so there's a real
header to click.

```typescript
import userEvent from '@testing-library/user-event'

it('collapsing a group hides its cards but keeps header and count', async () => {
  const onToggleCollapse = vi.fn()
  const groups = [
    { key: 'busy', label: 'Working', count: 1, sessions: [SESSIONS[0]] },
    { key: 'your-turn', label: 'Done', count: 1, sessions: [SESSIONS[1]] },
  ]
  const { rerender } = render(
    <SessionList {...baseProps} groups={groups} collapsedKeys={new Set()} onToggleCollapse={onToggleCollapse} />,
    { wrapper },
  )
  // expanded: the "Working" group's card is visible
  expect(screen.getByText(SESSIONS[0].name)).toBeInTheDocument()

  const header = screen.getByRole('button', { name: /Working/i })
  expect(header).toHaveAttribute('aria-expanded', 'true')
  await userEvent.click(header)
  expect(onToggleCollapse).toHaveBeenCalledWith('busy')

  // re-render as collapsed and assert the card is gone but header + count remain
  rerender(
    <SessionList {...baseProps} groups={groups} collapsedKeys={new Set(['busy'])} onToggleCollapse={onToggleCollapse} />,
  )
  expect(screen.queryByText(SESSIONS[0].name)).not.toBeInTheDocument()
  const collapsedHeader = screen.getByRole('button', { name: /Working/i })
  expect(collapsedHeader).toHaveAttribute('aria-expanded', 'false')
  expect(screen.getByText('1')).toBeInTheDocument() // count still shown
})
```

> Adjust `baseProps`, `SESSIONS`, `ACCOUNTS`, and the import of `vi`/`render`/`screen`
> to match the existing file (it already imports most of these). `userEvent` may already
> be imported — if so, don't duplicate it. Ensure the two fixture sessions have distinct
> `name` values; if `SESSIONS` has fewer than 2 entries, define a local 2-session array.

- [ ] **Step 2: Run test to verify it fails**

Run (from `web/`): `npx vitest run src/components/__tests__/SessionList.test.tsx`
Expected: FAIL — header is not a `button` / `aria-expanded` missing.

- [ ] **Step 3: Write minimal implementation**

Edit `web/src/components/SessionList.tsx`:

a) Add the chevron import at the top:

```typescript
import { ChevronRight, ChevronDown } from 'lucide-react'
```

b) Add two props to `SessionListProps` (after `onQuickResume`):

```typescript
  /** Keys of groups currently collapsed. Omit for all-expanded. */
  collapsedKeys?: Set<string>
  /** Toggle a group's collapsed state by its key. */
  onToggleCollapse?: (key: string) => void
```

c) Destructure them in the component signature (add alongside the other props):

```typescript
  collapsedKeys,
  onToggleCollapse,
```

d) Replace the grouped-render block (the `groups ? groups.map(...) : ...` JSX, currently
lines ~121-141) with a disclosure-button version:

```tsx
      {groups
        ? groups.map((group) => {
            const collapsed = collapsedKeys?.has(group.key) ?? false
            const panelId = `group-panel-${group.key}`
            return (
              <div key={group.key} className="space-y-2">
                {group.label !== null && (
                  <button
                    type="button"
                    onClick={() => onToggleCollapse?.(group.key)}
                    aria-expanded={!collapsed}
                    aria-controls={panelId}
                    className={cn(
                      'sticky top-0 z-10 flex w-full items-center gap-2 px-1 py-1',
                      'bg-gray-50/95 dark:bg-gray-900/95 backdrop-blur',
                      'text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400',
                      'hover:text-gray-700 dark:hover:text-gray-200',
                      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 rounded',
                    )}
                  >
                    {collapsed ? (
                      <ChevronRight size={14} aria-hidden />
                    ) : (
                      <ChevronDown size={14} aria-hidden />
                    )}
                    <span>{group.label}</span>
                    <span className="tabular-nums text-gray-400 dark:text-gray-500">{group.count}</span>
                  </button>
                )}
                {!collapsed && (
                  <div id={panelId} className="space-y-2">
                    {group.sessions.map(renderCard)}
                  </div>
                )}
              </div>
            )
          })
        : sessions.map(renderCard)}
```

> Rationale for dropping `role="presentation"`/`aria-hidden`: the header is now an
> interactive disclosure control and must be reachable by keyboard/AT. The list's
> `aria-activedescendant` virtual-focus model is unaffected — header buttons are real
> controls in tab order, exactly like the per-card overflow-menu buttons already inside
> this list.

e) Thread the props in **DesktopShell.tsx**:

Destructure (the `useSessionOrdering(...)` call ~line 106):
```typescript
  const { groupBy, sortBy, setGroupBy, setSortBy, groups, orderedSessions, collapsedKeys, toggleCollapse } =
    useSessionOrdering(sessions, sessionSearch.matchedIds)
```
Pass to `<SessionList>` (~line 391, add alongside `groups={groups}`):
```tsx
          collapsedKeys={collapsedKeys}
          onToggleCollapse={toggleCollapse}
```

f) Thread the props in **Dashboard.tsx** identically:

Destructure (~line 1237):
```typescript
  const { groupBy, sortBy, setGroupBy, setSortBy, groups, orderedSessions, collapsedKeys, toggleCollapse } =
    useSessionOrdering(sessions, sessionSearch.matchedIds)
```
Pass to `<SessionList>` (~line 1757, alongside `groups={groups}`):
```tsx
          collapsedKeys={collapsedKeys}
          onToggleCollapse={toggleCollapse}
```

- [ ] **Step 4: Run the SessionList test**

Run (from `web/`): `npx vitest run src/components/__tests__/SessionList.test.tsx`
Expected: PASS.

- [ ] **Step 5: Full gates**

Run (from `web/`):
```bash
npm test
npx tsc -b --force
npm run lint
```
Expected: all green. (`tsc -b --force` is the real type check; the threaded props must
typecheck in both DesktopShell and Dashboard.)

- [ ] **Step 6: Commit**

```bash
git add web/src/components/SessionList.tsx web/src/components/DesktopShell.tsx web/src/components/Dashboard.tsx web/src/components/__tests__/SessionList.test.tsx
git commit -m "feat(web): collapsible session-group headers in the session list"
```

---

## Self-Review

**Spec coverage:**
- Persistence (decision 1) → Task 1 (`groupCollapse.ts`) + Task 2 (state/effect).
- Default expanded + per-group toggle, no bulk control (decision 2) → empty-set default (Task 1), `toggleCollapse` (Task 2), per-header button (Task 3); no bulk control added. ✓ YAGNI.
- Collapsed shows label + count (decision 3) → Task 3 header renders label + count regardless of `collapsed`. ✓
- Scope = grouping active only (decision 4) → header only rendered when `group.label !== null`; `None` mode untouched. ✓
- Keyboard-nav correctness → Task 2 `orderedSessions` excludes collapsed groups. ✓
- Accessibility (disclosure button, aria-expanded/controls) → Task 3. ✓
- Edge cases (blocked/malformed storage, stale keys, non-string entries) → Task 1 tests. ✓

**Placeholder scan:** no TBD/TODO; all code shown in full. The "confirm tier key" and
"match existing fixtures" notes are verification instructions for the implementer against
named files, not deferred work. ✓

**Type consistency:** `collapsedKeys: Set<string>` and `toggleCollapse: (key: string) => void`
are named identically across the hook interface (Task 2), the props (Task 3), and both
consumers. The `SessionList` prop is `onToggleCollapse`, wired to the hook's
`toggleCollapse` at the call sites. ✓
