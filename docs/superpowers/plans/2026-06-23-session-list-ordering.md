# Session List Ordering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add orthogonal Group-by (None/Repo/Status) and Sort-by (Status tier/Last active/Age/Name) controls to the dashboard sessions list on both layouts, persisted across reloads.

**Architecture:** A pure `orderSessions` module produces grouped+sorted output; a `useSessionOrdering` hook owns state + localStorage persistence and is shared by both list layouts (`Dashboard` <1024px and `DesktopShell` ≥1024px); `SessionList` gains an additive optional `groups` prop that renders section headers.

**Tech Stack:** React 18 + TypeScript, Vite, Vitest + Testing Library, Tailwind. Frontend only — no backend/API changes.

## Global Constraints

- Run all web commands from `web/`: `vitest run`, `tsc --noEmit`, `eslint`.
- Reuse existing sources of truth — do NOT add parallel maps:
  - Status order/tiers: `web/src/session/statusTier.ts` (`TIER_RANK`, `statusTier`, `StatusTier`).
  - Repo label + Scratch bucket: `web/src/lib/repoLabel.ts` (`formatRepoLabel`).
  - Timestamp validity: `web/src/lib/relativeTime.ts` (`isValidTimestamp`).
  - Persisted-pref pattern: mirror `web/src/lib/lastRepo.ts` (try/catch-wrapped localStorage).
- Native `<select>` styled with Tailwind (house style; there is no shadcn Select primitive).
- Default selection (`groupBy: 'none'`, `sortBy: 'status'`) must reproduce today's `Dashboard` order exactly (parity invariant in Task 1).
- Commit after each task. Branch: `feat/session-list-ordering` (already created off `origin/master`).

---

### Task 1: Pure ordering module + tier labels

**Files:**
- Create: `web/src/session/ordering.ts`
- Modify: `web/src/session/statusTier.ts` (add `TIER_LABEL`)
- Test: `web/src/session/__tests__/ordering.test.ts`

**Interfaces:**
- Consumes: `Session` from `@/api/types`; `TIER_RANK`, `statusTier`, `StatusTier` from `../statusTier`; `formatRepoLabel` from `@/lib/repoLabel`; `isValidTimestamp` from `@/lib/relativeTime`.
- Produces:
  - `type GroupBy = 'none' | 'repo' | 'status'`
  - `type SortBy = 'status' | 'lastActive' | 'age' | 'name'`
  - `interface SessionGroup { key: string; label: string | null; count: number; sessions: Session[] }`
  - `function orderSessions(sessions: readonly Session[], groupBy: GroupBy, sortBy: SortBy): SessionGroup[]`
  - `TIER_LABEL: Record<StatusTier, string>` (in `statusTier.ts`)

- [ ] **Step 1: Add `TIER_LABEL` to `statusTier.ts`**

Append below `TIER_RANK` in `web/src/session/statusTier.ts`:

```ts
/** Human-readable section header for each tier (group-by-status). */
export const TIER_LABEL: Record<StatusTier, string> = {
  'needs-you': 'Needs you',
  error: 'Needs attention',
  'your-turn': 'Your turn',
  busy: 'Working',
}
```

- [ ] **Step 2: Write the failing test**

Create `web/src/session/__tests__/ordering.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { orderSessions } from '../ordering'
import { sortSessionsByTier } from '../statusTier'
import type { Session } from '@/api/types'

function mk(over: Partial<Session> & { id: string }): Session {
  return {
    name: over.id, accountName: 'a', status: 'idle',
    createdAt: '2026-06-01T00:00:00Z', updatedAt: '2026-06-01T00:00:00Z',
    ...over,
  }
}

const flat = (s: Session[], g: Parameters<typeof orderSessions>[1], so: Parameters<typeof orderSessions>[2]) =>
  orderSessions(s, g, so).flatMap((grp) => grp.sessions).map((x) => x.id)

describe('orderSessions', () => {
  it('parity: none+status equals sortSessionsByTier', () => {
    const s = [
      mk({ id: 'a', status: 'idle' }),
      mk({ id: 'b', status: 'waiting_input' }),
      mk({ id: 'c', status: 'in_progress' }),
      mk({ id: 'd', status: 'needs_attention' }),
    ]
    expect(flat(s, 'none', 'status')).toEqual(sortSessionsByTier(s).map((x) => x.id))
  })

  it('lastActive: newest first, null/invalid sinks to bottom', () => {
    const s = [
      mk({ id: 'old', updatedAt: '2026-06-01T00:00:00Z' }),
      mk({ id: 'new', updatedAt: '2026-06-20T00:00:00Z' }),
      mk({ id: 'none', updatedAt: null }),
    ]
    expect(flat(s, 'none', 'lastActive')).toEqual(['new', 'old', 'none'])
  })

  it('age: oldest created first', () => {
    const s = [
      mk({ id: 'y', createdAt: '2026-06-10T00:00:00Z' }),
      mk({ id: 'x', createdAt: '2026-06-01T00:00:00Z' }),
    ]
    expect(flat(s, 'none', 'age')).toEqual(['x', 'y'])
  })

  it('name: case-insensitive A→Z with id tie-break', () => {
    const s = [mk({ id: '2', name: 'beta' }), mk({ id: '1', name: 'Alpha' })]
    expect(flat(s, 'none', 'name')).toEqual(['1', '2'])
  })

  it('group by status: groups ordered by TIER_RANK with labels', () => {
    const s = [mk({ id: 'busy', status: 'in_progress' }), mk({ id: 'need', status: 'waiting_input' })]
    const g = orderSessions(s, 'status', 'status')
    expect(g.map((x) => x.label)).toEqual(['Needs you', 'Working'])
    expect(g.map((x) => x.count)).toEqual([1, 1])
  })

  it('group by repo: alphabetical, Scratch last', () => {
    const s = [
      mk({ id: 'z', repo: 'zeta/z' }),
      mk({ id: 's', repo: null }),
      mk({ id: 'a', repo: 'alpha/a' }),
    ]
    const g = orderSessions(s, 'repo', 'name')
    expect(g.map((x) => x.label)).toEqual(['alpha/a', 'zeta/z', 'Scratch'])
  })

  it('none → single unlabeled group', () => {
    const g = orderSessions([mk({ id: 'a' })], 'none', 'status')
    expect(g).toHaveLength(1)
    expect(g[0].label).toBeNull()
  })
})
```

- [ ] **Step 3: Run test to verify it fails**

Run (from `web/`): `npx vitest run src/session/__tests__/ordering.test.ts`
Expected: FAIL — `orderSessions` is not exported / module not found.

- [ ] **Step 4: Write the implementation**

Create `web/src/session/ordering.ts`:

```ts
import type { Session } from '@/api/types'
import { statusTier, TIER_RANK, TIER_LABEL, type StatusTier } from './statusTier'
import { formatRepoLabel } from '@/lib/repoLabel'
import { isValidTimestamp } from '@/lib/relativeTime'

export type GroupBy = 'none' | 'repo' | 'status'
export type SortBy = 'status' | 'lastActive' | 'age' | 'name'

export interface SessionGroup {
  key: string
  label: string | null
  count: number
  sessions: Session[]
}

const byId = (a: Session, b: Session): number => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)

const lastActiveMs = (iso: string | null | undefined): number | null =>
  isValidTimestamp(iso) ? new Date(iso).getTime() : null

// Within-group comparators. Every non-status comparator ends with an id
// tie-break for stability across refetches. `status` intentionally omits it so
// it stays byte-identical to sortSessionsByTier (stable sort → input order).
const COMPARATORS: Record<SortBy, (a: Session, b: Session) => number> = {
  status: (a, b) => TIER_RANK[statusTier(a.status)] - TIER_RANK[statusTier(b.status)],
  lastActive: (a, b) => {
    const ta = lastActiveMs(a.updatedAt)
    const tb = lastActiveMs(b.updatedAt)
    if (ta === null && tb === null) return byId(a, b)
    if (ta === null) return 1
    if (tb === null) return -1
    return tb - ta || byId(a, b)
  },
  age: (a, b) => new Date(a.createdAt).getTime() - new Date(b.createdAt).getTime() || byId(a, b),
  name: (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }) || byId(a, b),
}

export function orderSessions(
  sessions: readonly Session[],
  groupBy: GroupBy,
  sortBy: SortBy,
): SessionGroup[] {
  const sorted = [...sessions].sort(COMPARATORS[sortBy])

  if (groupBy === 'none') {
    return [{ key: 'all', label: null, count: sorted.length, sessions: sorted }]
  }

  if (groupBy === 'status') {
    const tiers = (['needs-you', 'error', 'your-turn', 'busy'] as StatusTier[]).sort(
      (a, b) => TIER_RANK[a] - TIER_RANK[b],
    )
    const groups: SessionGroup[] = []
    for (const tier of tiers) {
      const inTier = sorted.filter((s) => statusTier(s.status) === tier)
      if (inTier.length) {
        groups.push({ key: tier, label: TIER_LABEL[tier], count: inTier.length, sessions: inTier })
      }
    }
    return groups
  }

  // groupBy === 'repo'
  const buckets = new Map<string, { label: string; isScratch: boolean; sessions: Session[] }>()
  for (const s of sorted) {
    const rl = formatRepoLabel(s.repo)
    const bucket = buckets.get(rl.text) ?? { label: rl.text, isScratch: rl.isScratch, sessions: [] }
    bucket.sessions.push(s)
    buckets.set(rl.text, bucket)
  }
  return [...buckets.values()]
    .sort(
      (a, b) =>
        (a.isScratch ? 1 : 0) - (b.isScratch ? 1 : 0) ||
        a.label.localeCompare(b.label, undefined, { sensitivity: 'base' }),
    )
    .map((b) => ({ key: b.label, label: b.label, count: b.sessions.length, sessions: b.sessions }))
}
```

- [ ] **Step 5: Run tests + typecheck + lint**

Run (from `web/`):
```
npx vitest run src/session/__tests__/ordering.test.ts
npx tsc --noEmit
npx eslint src/session/ordering.ts src/session/statusTier.ts
```
Expected: tests PASS, tsc clean, eslint clean.

- [ ] **Step 6: Commit**

```bash
git add web/src/session/ordering.ts web/src/session/statusTier.ts web/src/session/__tests__/ordering.test.ts
git commit -m "feat(sessions): pure orderSessions group/sort module + tier labels

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: localStorage persistence helper

**Files:**
- Create: `web/src/lib/sessionOrder.ts`
- Test: `web/src/lib/__tests__/sessionOrder.test.ts`

**Interfaces:**
- Consumes: `GroupBy`, `SortBy` from `@/session/ordering`.
- Produces:
  - `interface SessionOrderPref { groupBy: GroupBy; sortBy: SortBy }`
  - `function loadSessionOrder(): SessionOrderPref` (defaults `{ groupBy: 'none', sortBy: 'status' }`)
  - `function saveSessionOrder(pref: SessionOrderPref): void`

- [ ] **Step 1: Write the failing test**

Create `web/src/lib/__tests__/sessionOrder.test.ts`:

```ts
import { describe, it, expect, beforeEach } from 'vitest'
import { loadSessionOrder, saveSessionOrder } from '../sessionOrder'

describe('sessionOrder persistence', () => {
  beforeEach(() => window.localStorage.clear())

  it('defaults to none/status when nothing stored', () => {
    expect(loadSessionOrder()).toEqual({ groupBy: 'none', sortBy: 'status' })
  })

  it('round-trips a saved selection', () => {
    saveSessionOrder({ groupBy: 'repo', sortBy: 'lastActive' })
    expect(loadSessionOrder()).toEqual({ groupBy: 'repo', sortBy: 'lastActive' })
  })

  it('falls back to defaults on corrupt JSON', () => {
    window.localStorage.setItem('ccswitch.sessionOrder', '{not json')
    expect(loadSessionOrder()).toEqual({ groupBy: 'none', sortBy: 'status' })
  })

  it('rejects unknown enum values', () => {
    window.localStorage.setItem('ccswitch.sessionOrder', JSON.stringify({ groupBy: 'x', sortBy: 'y' }))
    expect(loadSessionOrder()).toEqual({ groupBy: 'none', sortBy: 'status' })
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `web/`): `npx vitest run src/lib/__tests__/sessionOrder.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

Create `web/src/lib/sessionOrder.ts`:

```ts
/**
 * Persists the dashboard session-list group/sort selection. Mirrors lastRepo.ts:
 * all access is try/catch-wrapped so a blocked/unavailable localStorage degrades
 * to the defaults instead of crashing. Unknown enum values (e.g. from a future
 * build) also fall back to defaults.
 */
import type { GroupBy, SortBy } from '@/session/ordering'

const KEY = 'ccswitch.sessionOrder'
const GROUP_BY: GroupBy[] = ['none', 'repo', 'status']
const SORT_BY: SortBy[] = ['status', 'lastActive', 'age', 'name']

export interface SessionOrderPref {
  groupBy: GroupBy
  sortBy: SortBy
}

const DEFAULT: SessionOrderPref = { groupBy: 'none', sortBy: 'status' }

export function loadSessionOrder(): SessionOrderPref {
  try {
    const raw = window.localStorage.getItem(KEY)
    if (!raw) return { ...DEFAULT }
    const parsed = JSON.parse(raw) as Partial<SessionOrderPref>
    return {
      groupBy: GROUP_BY.includes(parsed.groupBy as GroupBy) ? (parsed.groupBy as GroupBy) : DEFAULT.groupBy,
      sortBy: SORT_BY.includes(parsed.sortBy as SortBy) ? (parsed.sortBy as SortBy) : DEFAULT.sortBy,
    }
  } catch {
    return { ...DEFAULT }
  }
}

export function saveSessionOrder(pref: SessionOrderPref): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(pref))
  } catch {
    /* ignore — persistence is best-effort */
  }
}
```

- [ ] **Step 4: Run tests + typecheck + lint**

Run (from `web/`):
```
npx vitest run src/lib/__tests__/sessionOrder.test.ts
npx tsc --noEmit
npx eslint src/lib/sessionOrder.ts
```
Expected: PASS, clean, clean.

- [ ] **Step 5: Commit**

```bash
git add web/src/lib/sessionOrder.ts web/src/lib/__tests__/sessionOrder.test.ts
git commit -m "feat(sessions): persist session-list group/sort selection

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `useSessionOrdering` shared hook

**Files:**
- Create: `web/src/session/useSessionOrdering.ts`
- Test: `web/src/session/__tests__/useSessionOrdering.test.ts`

**Interfaces:**
- Consumes: `orderSessions`, `GroupBy`, `SortBy`, `SessionGroup` from `./ordering`; `loadSessionOrder`, `saveSessionOrder` from `@/lib/sessionOrder`; `Session` from `@/api/types`.
- Produces:
  - `interface SessionOrdering { groupBy: GroupBy; sortBy: SortBy; setGroupBy: (g: GroupBy) => void; setSortBy: (s: SortBy) => void; groups: SessionGroup[]; orderedSessions: Session[] }`
  - `function useSessionOrdering(sessions: readonly Session[]): SessionOrdering`

- [ ] **Step 1: Write the failing test**

Create `web/src/session/__tests__/useSessionOrdering.test.ts`:

```ts
import { describe, it, expect, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useSessionOrdering } from '../useSessionOrdering'
import type { Session } from '@/api/types'

function mk(id: string, over: Partial<Session> = {}): Session {
  return {
    id, name: id, accountName: 'a', status: 'idle',
    createdAt: '2026-06-01T00:00:00Z', updatedAt: '2026-06-01T00:00:00Z', ...over,
  }
}

describe('useSessionOrdering', () => {
  beforeEach(() => window.localStorage.clear())

  it('defaults to none/status and flattens groups into orderedSessions', () => {
    const sessions = [mk('a'), mk('b')]
    const { result } = renderHook(() => useSessionOrdering(sessions))
    expect(result.current.groupBy).toBe('none')
    expect(result.current.sortBy).toBe('status')
    expect(result.current.orderedSessions.map((s) => s.id))
      .toEqual(result.current.groups.flatMap((g) => g.sessions).map((s) => s.id))
  })

  it('setGroupBy re-derives groups and persists', () => {
    const { result } = renderHook(() => useSessionOrdering([mk('a', { repo: 'o/r' })]))
    act(() => result.current.setGroupBy('repo'))
    expect(result.current.groups[0].label).toBe('o/r')
    expect(window.localStorage.getItem('ccswitch.sessionOrder')).toContain('repo')
  })

  it('restores persisted selection on mount', () => {
    window.localStorage.setItem('ccswitch.sessionOrder', JSON.stringify({ groupBy: 'status', sortBy: 'name' }))
    const { result } = renderHook(() => useSessionOrdering([mk('a')]))
    expect(result.current.groupBy).toBe('status')
    expect(result.current.sortBy).toBe('name')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `web/`): `npx vitest run src/session/__tests__/useSessionOrdering.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

Create `web/src/session/useSessionOrdering.ts`:

```ts
import { useEffect, useMemo, useState } from 'react'
import type { Session } from '@/api/types'
import { orderSessions, type GroupBy, type SortBy, type SessionGroup } from './ordering'
import { loadSessionOrder, saveSessionOrder } from '@/lib/sessionOrder'

export interface SessionOrdering {
  groupBy: GroupBy
  sortBy: SortBy
  setGroupBy: (g: GroupBy) => void
  setSortBy: (s: SortBy) => void
  groups: SessionGroup[]
  orderedSessions: Session[]
}

/**
 * Shared group/sort state for the session list, used by both layouts
 * (DesktopShell ≥1024px and Dashboard <1024px) so ordering stays consistent and
 * the logic lives in one place. State is restored from / persisted to
 * localStorage.
 */
export function useSessionOrdering(sessions: readonly Session[]): SessionOrdering {
  const initial = loadSessionOrder()
  const [groupBy, setGroupBy] = useState<GroupBy>(initial.groupBy)
  const [sortBy, setSortBy] = useState<SortBy>(initial.sortBy)

  useEffect(() => {
    saveSessionOrder({ groupBy, sortBy })
  }, [groupBy, sortBy])

  const groups = useMemo(() => orderSessions(sessions, groupBy, sortBy), [sessions, groupBy, sortBy])
  const orderedSessions = useMemo(() => groups.flatMap((g) => g.sessions), [groups])

  return { groupBy, sortBy, setGroupBy, setSortBy, groups, orderedSessions }
}
```

- [ ] **Step 4: Run tests + typecheck + lint**

Run (from `web/`):
```
npx vitest run src/session/__tests__/useSessionOrdering.test.ts
npx tsc --noEmit
npx eslint src/session/useSessionOrdering.ts
```
Expected: PASS, clean, clean.

- [ ] **Step 5: Commit**

```bash
git add web/src/session/useSessionOrdering.ts web/src/session/__tests__/useSessionOrdering.test.ts
git commit -m "feat(sessions): shared useSessionOrdering hook

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: `SessionListControls` component

**Files:**
- Create: `web/src/components/SessionListControls.tsx`
- Test: `web/src/components/__tests__/SessionListControls.test.tsx`

**Interfaces:**
- Consumes: `GroupBy`, `SortBy` from `@/session/ordering`.
- Produces: `function SessionListControls(props: { groupBy: GroupBy; sortBy: SortBy; onGroupByChange: (g: GroupBy) => void; onSortByChange: (s: SortBy) => void }): JSX.Element`

- [ ] **Step 1: Write the failing test**

Create `web/src/components/__tests__/SessionListControls.test.tsx`:

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SessionListControls } from '../SessionListControls'

describe('SessionListControls', () => {
  it('renders current selection and emits changes', async () => {
    const onGroupByChange = vi.fn()
    const onSortByChange = vi.fn()
    render(
      <SessionListControls
        groupBy="none"
        sortBy="status"
        onGroupByChange={onGroupByChange}
        onSortByChange={onSortByChange}
      />,
    )
    await userEvent.selectOptions(screen.getByLabelText('Group by'), 'repo')
    expect(onGroupByChange).toHaveBeenCalledWith('repo')
    await userEvent.selectOptions(screen.getByLabelText('Sort by'), 'lastActive')
    expect(onSortByChange).toHaveBeenCalledWith('lastActive')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `web/`): `npx vitest run src/components/__tests__/SessionListControls.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

Create `web/src/components/SessionListControls.tsx`:

```tsx
import { cn } from '@/lib/utils'
import type { GroupBy, SortBy } from '@/session/ordering'

interface SessionListControlsProps {
  groupBy: GroupBy
  sortBy: SortBy
  onGroupByChange: (g: GroupBy) => void
  onSortByChange: (s: SortBy) => void
}

const selectClass = cn(
  'text-xs rounded-md border border-gray-200 dark:border-gray-700',
  'bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300',
  'px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
)

export function SessionListControls({
  groupBy,
  sortBy,
  onGroupByChange,
  onSortByChange,
}: SessionListControlsProps) {
  return (
    <div className="flex items-center gap-3 flex-wrap">
      <label className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400">
        <span>Group by</span>
        <select
          aria-label="Group by"
          className={selectClass}
          value={groupBy}
          onChange={(e) => onGroupByChange(e.target.value as GroupBy)}
        >
          <option value="none">None</option>
          <option value="repo">Repo</option>
          <option value="status">Status</option>
        </select>
      </label>
      <label className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400">
        <span>Sort by</span>
        <select
          aria-label="Sort by"
          className={selectClass}
          value={sortBy}
          onChange={(e) => onSortByChange(e.target.value as SortBy)}
        >
          <option value="status">Status tier</option>
          <option value="lastActive">Last active</option>
          <option value="age">Age</option>
          <option value="name">Name</option>
        </select>
      </label>
    </div>
  )
}
```

- [ ] **Step 4: Run tests + typecheck + lint**

Run (from `web/`):
```
npx vitest run src/components/__tests__/SessionListControls.test.tsx
npx tsc --noEmit
npx eslint src/components/SessionListControls.tsx
```
Expected: PASS, clean, clean.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/SessionListControls.tsx web/src/components/__tests__/SessionListControls.test.tsx
git commit -m "feat(sessions): SessionListControls group/sort selectors

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: `SessionList` optional `groups` prop + headers

**Files:**
- Modify: `web/src/components/SessionList.tsx`
- Test: `web/src/components/__tests__/SessionList.test.tsx` (add cases; do not change existing ones)

**Interfaces:**
- Consumes: `SessionGroup` from `@/session/ordering`.
- Produces: `SessionList` now also accepts an optional `groups?: SessionGroup[]`. When present it renders grouped (with headers for non-null labels); when absent it renders the flat `sessions` exactly as today.

- [ ] **Step 1: Write the failing test (new cases)**

Append to `web/src/components/__tests__/SessionList.test.tsx` inside the top-level `describe('SessionList', …)`:

```tsx
  it('renders group headers with counts when groups provided', () => {
    const groups = [
      { key: 'needs-you', label: 'Needs you', count: 1, sessions: [SESSIONS[0]] },
      { key: 'your-turn', label: 'Your turn', count: 1, sessions: [SESSIONS[1]] },
    ]
    render(
      <SessionList
        sessions={SESSIONS}
        groups={groups}
        accounts={ACCOUNTS}
        onNew={() => {}}
        onOpen={() => {}}
        onRename={() => {}}
        onClose={() => {}}
        onDelete={() => {}}
      />,
      { wrapper },
    )
    expect(screen.getByText('Needs you')).toBeInTheDocument()
    expect(screen.getByText('Your turn')).toBeInTheDocument()
    // both rows still render in flat order
    expect(screen.getByText('Work session')).toBeInTheDocument()
    expect(screen.getByText('Research')).toBeInTheDocument()
  })

  it('renders no header for a null-label group', () => {
    const groups = [{ key: 'all', label: null, count: SESSIONS.length, sessions: SESSIONS }]
    render(
      <SessionList
        sessions={SESSIONS}
        groups={groups}
        accounts={ACCOUNTS}
        onNew={() => {}}
        onOpen={() => {}}
        onRename={() => {}}
        onClose={() => {}}
        onDelete={() => {}}
      />,
      { wrapper },
    )
    expect(screen.queryByRole('presentation')).not.toBeInTheDocument()
    expect(screen.getByText('Work session')).toBeInTheDocument()
  })
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `web/`): `npx vitest run src/components/__tests__/SessionList.test.tsx`
Expected: FAIL — `groups` prop not accepted / headers not rendered.

- [ ] **Step 3: Implement the additive prop**

In `web/src/components/SessionList.tsx`:

(a) Add the import at the top:
```tsx
import type { SessionGroup } from '@/session/ordering'
```

(b) Add `groups` to `SessionListProps`:
```tsx
  sessions: Session[]
  /** When provided, render grouped with section headers; else render flat `sessions`. */
  groups?: SessionGroup[]
  accounts: Account[]
```

(c) Destructure `groups` in the function signature (add after `sessions`):
```tsx
export function SessionList({
  sessions,
  groups,
  accounts,
  isLoading,
  selectedId,
  onNew,
  onOpen,
  onRename,
  onClose,
  onDelete,
}: SessionListProps) {
```

(d) Replace the final `return (...)` block's children. Keep the wrapping
`<div id="session-listbox" …>` exactly as-is, but render either grouped or flat.
Extract the per-card render into a local helper and branch:

```tsx
  const renderCard = (session: Session) => (
    <SessionCard
      key={session.id}
      session={session}
      account={accountMap.get(session.accountName)}
      selected={session.id === selectedId}
      onOpen={onOpen}
      onRename={onRename}
      onClose={onClose}
      onDelete={onDelete}
    />
  )

  return (
    <div
      id="session-listbox"
      aria-label="Sessions"
      aria-activedescendant={selectedId ? `row-${selectedId}` : undefined}
      className="space-y-2"
    >
      {groups
        ? groups.map((group) => (
            <div key={group.key} className="space-y-2">
              {group.label !== null && (
                <div
                  role="presentation"
                  aria-hidden="true"
                  className={cn(
                    'sticky top-0 z-10 flex items-center gap-2 px-1 py-1',
                    'bg-gray-50/95 dark:bg-gray-900/95 backdrop-blur',
                    'text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400',
                  )}
                >
                  <span>{group.label}</span>
                  <span className="tabular-nums text-gray-400 dark:text-gray-500">{group.count}</span>
                </div>
              )}
              {group.sessions.map(renderCard)}
            </div>
          ))
        : sessions.map(renderCard)}
    </div>
  )
```

Note: the empty/loading guards above this block are unchanged. Grouped mode
still relies on `sessions.length === 0` (the EmptyState guard) — callers pass the
same underlying list, so an empty list short-circuits before this block.

- [ ] **Step 4: Run the full SessionList + a11y suites**

Run (from `web/`):
```
npx vitest run src/components/__tests__/SessionList.test.tsx src/components/__tests__/a11y.test.tsx
npx tsc --noEmit
npx eslint src/components/SessionList.tsx
```
Expected: ALL pass (existing flat-`sessions` cases unchanged), clean, clean.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/SessionList.tsx web/src/components/__tests__/SessionList.test.tsx
git commit -m "feat(sessions): SessionList optional grouped rendering

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Wire into `Dashboard.tsx`

**Files:**
- Modify: `web/src/components/Dashboard.tsx`
- Test: `web/src/components/__tests__/DashboardTabs.test.tsx` (add a grouping case) — or a focused new test if the existing file's harness doesn't fit.

**Interfaces:**
- Consumes: `useSessionOrdering` from `@/session/useSessionOrdering`; `SessionListControls` from `./SessionListControls`.
- Produces: no new exports; `Dashboard` renders controls + grouped list.

- [ ] **Step 1: Write the failing test**

Add to `web/src/components/__tests__/DashboardTabs.test.tsx` (reuse its existing render helper/imports; if it renders `<Dashboard>` with a QueryClient + seeded `useSessions`, follow that pattern). The assertion:

```tsx
  it('groups the session list by status when selected', async () => {
    // (render Dashboard via the file's existing helper with ≥1 session seeded)
    await userEvent.selectOptions(await screen.findByLabelText('Group by'), 'status')
    // a status section header appears (e.g. the seeded session is idle → "Your turn")
    expect(await screen.findByText('Your turn')).toBeInTheDocument()
  })
```

If `DashboardTabs.test.tsx` has no seeded-sessions harness, instead create
`web/src/components/__tests__/SessionGrouping.test.tsx` that mocks
`@/api/hooks` `useSessions` to return two sessions (mirror the mock style already
used in the dashboard tests — see existing `vi.mock('@/api/hooks', …)` usages)
and asserts the same.

- [ ] **Step 2: Run test to verify it fails**

Run (from `web/`): `npx vitest run src/components/__tests__/DashboardTabs.test.tsx` (or the new file)
Expected: FAIL — no "Group by" control yet.

- [ ] **Step 3: Implement the wiring**

In `web/src/components/Dashboard.tsx`:

(a) Add imports near the other component imports:
```tsx
import { SessionListControls } from './SessionListControls'
import { useSessionOrdering } from '@/session/useSessionOrdering'
```

(b) Remove the now-unused `sortSessionsByTier` import (line ~39) **only if no
other usage remains** — grep first: `grep -n sortSessionsByTier web/src/components/Dashboard.tsx`. If Task 1's parity test is the only other reference, the import here is unused after this change; remove it to satisfy eslint.

(c) Replace line ~1041:
```tsx
const orderedSessions = useMemo(() => sortSessionsByTier(sessions), [sessions])
```
with:
```tsx
const { groupBy, sortBy, setGroupBy, setSortBy, groups, orderedSessions } = useSessionOrdering(sessions)
```

(d) In the sessions-tab JSX, render the controls immediately above
`<SessionList …>` (the block at ~1483) and pass `groups`. Wrap so controls only
show when there are sessions:
```tsx
          {sessions.length > 0 && (
            <SessionListControls
              groupBy={groupBy}
              sortBy={sortBy}
              onGroupByChange={setGroupBy}
              onSortByChange={setSortBy}
            />
          )}
          <SessionList
            sessions={orderedSessions}
            groups={groups}
            accounts={accounts}
            isLoading={isLoading}
            selectedId={selectedId}
            onNew={openNewDialog}
            onOpen={handleOpen}
            onRename={handleRename}
            onClose={handleClose}
            onDelete={handleSessionDelete}
          />
```

- [ ] **Step 4: Run the dashboard suites + typecheck + lint**

Run (from `web/`):
```
npx vitest run src/components/__tests__/DashboardTabs.test.tsx src/components/__tests__/KeyboardSessions.test.tsx
npx tsc --noEmit
npx eslint src/components/Dashboard.tsx
```
Expected: PASS (keyboard nav still works against `orderedSessions`), clean, clean.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/Dashboard.tsx web/src/components/__tests__/
git commit -m "feat(sessions): wire group/sort controls into Dashboard

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Wire into `DesktopShell.tsx`

**Files:**
- Modify: `web/src/components/DesktopShell.tsx`
- Test: `web/src/components/__tests__/` — add a DesktopShell grouping/nav case (mirror the existing DesktopShell test harness if present; else add to the new `SessionGrouping.test.tsx`).

**Interfaces:**
- Consumes: `useSessionOrdering` from `@/session/useSessionOrdering`; `SessionListControls` from `./SessionListControls`.
- Produces: no new exports.

- [ ] **Step 1: Write the failing test**

Add a case asserting that, in `DesktopShell`, selecting Group by → Status renders
a status header, and that ↑/↓ keyboard navigation still highlights rows (i.e. nav
consumes `orderedSessions`). If there is an existing `DesktopShell` test file,
extend it; otherwise add to `SessionGrouping.test.tsx` rendering `<DesktopShell/>`
behind the same `useSessions` mock and a `useMediaQuery` mock returning `true`.

```tsx
  it('DesktopShell: groups by status and keeps keyboard nav', async () => {
    // render DesktopShell with two seeded sessions
    await userEvent.selectOptions(await screen.findByLabelText('Group by'), 'status')
    expect(await screen.findByText(/Your turn|Working|Needs you/)).toBeInTheDocument()
  })
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `web/`): `npx vitest run src/components/__tests__/SessionGrouping.test.tsx`
Expected: FAIL — no "Group by" control in DesktopShell yet.

- [ ] **Step 3: Implement the wiring**

In `web/src/components/DesktopShell.tsx`:

(a) Add imports:
```tsx
import { SessionListControls } from './SessionListControls'
import { useSessionOrdering } from '@/session/useSessionOrdering'
```

(b) After `const { data: sessions = [], isLoading: sessionsLoading } = useSessions()`
(line ~113), add:
```tsx
  const { groupBy, sortBy, setGroupBy, setSortBy, groups, orderedSessions } = useSessionOrdering(sessions)
```

(c) Replace **every** keyboard-nav reference to the raw `sessions` array with
`orderedSessions`, so the highlight index maps to the displayed order. Update
these spots (verify with `grep -n 'sessions\[' web/src/components/DesktopShell.tsx`
and the lines noted in the spec ~134/147/178/216/227/247/251/282):
- `highlighted = … sessions[highlightedIndex]` → `orderedSessions[highlightedIndex]`
- `clampedIndex … sessions.length` → `orderedSessions.length`
- the `sessions.findIndex(...)` / `sessions.find(...)` used for selection sync and row actions → `orderedSessions.findIndex/find`
- the up/down handlers using `sessions.length` and `sessions[action.index]` → `orderedSessions`

Leave the `useSessions()` destructure and `sessionsLoading` as-is. Do NOT change
the data source — only the ordering used for nav/lookup.

(d) Render the controls above the `<SessionList>` (the block at ~381), and pass
`groups`. Inside the existing scroll container, after the "New Session" button:
```tsx
        {sessions.length > 0 && (
          <SessionListControls
            groupBy={groupBy}
            sortBy={sortBy}
            onGroupByChange={setGroupBy}
            onSortByChange={setSortBy}
          />
        )}
        <SessionList
          sessions={orderedSessions}
          groups={groups}
          accounts={accounts}
          isLoading={sessionsLoading}
          selectedId={selectedSessionId ?? highlighted?.id}
          onNew={focusNew}
          onOpen={focusSession}
          onRename={handleRename}
          onClose={handleClose}
          onDelete={handleDelete}
        />
```

- [ ] **Step 4: Run suites + typecheck + lint + full web suite**

Run (from `web/`):
```
npx vitest run
npx tsc --noEmit
npx eslint src/components/DesktopShell.tsx
```
Expected: full suite PASS, clean, clean. (A full `vitest run` here because Task 7
touches shared nav logic — confirm nothing regressed.)

- [ ] **Step 5: Commit**

```bash
git add web/src/components/DesktopShell.tsx web/src/components/__tests__/
git commit -m "feat(sessions): wire group/sort controls into DesktopShell

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final verification (after Task 7)

Run from `web/`:
```
npx vitest run
npx tsc --noEmit
npx eslint src
```
All green → push branch and open a PR per CLAUDE.md completion workflow (non-draft,
`gh pr merge <#> --squash --auto`). The PR body should note: frontend-only;
default order unchanged on `Dashboard`; `DesktopShell` default order changes from
raw to status-tier (intended consistency fix).

## Self-review notes

- **Spec coverage:** orthogonal controls (T4), Group None/Repo/Status + Sort
  status/lastActive/age/name (T1), within-group sort (T1), persistence (T2/T3),
  parity invariant (T1), both layouts via shared hook (T3/T6/T7), headers+counts
  (T5), keyboard nav across boundaries (T6/T7), null repo→Scratch & null
  updatedAt→bottom (T1). All covered.
- **Reuse:** `TIER_RANK`/`statusTier`/`TIER_LABEL`, `formatRepoLabel`,
  `isValidTimestamp`, `lastRepo.ts` pattern — no parallel implementations.
- **Type consistency:** `GroupBy`/`SortBy`/`SessionGroup`/`SessionOrdering`
  names identical across T1→T7.
