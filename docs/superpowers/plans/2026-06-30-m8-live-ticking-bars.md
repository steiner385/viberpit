# M-Track-8: Live-ticking progress bars Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make running pipeline bars (per-check drill-down rows AND the collapsed CI-node connector band) advance smoothly every second between polls, ticking off the existing `/api/prs` snapshot + react-query's `dataUpdatedAt` — no backend or wire change.

**Architecture:** A `useNow(1000)` hook (composing the existing `usePageVisible`) re-renders once a second while the tab is visible. Two pure helpers (`liveElapsedS`, `isOverrun`) turn a snapshot elapsed + poll receive-time + now into a live elapsed. `dataUpdatedAt` is threaded `PipelineView → PipelineBoard → StageTrack` and `→ CheckDrillDown → CheckRowList`; each leaf recomputes its band from the live elapsed and snaps to server truth on the next poll.

**Tech Stack:** React 18 + TypeScript + Vite + Tailwind, Vitest + React Testing Library, react-query (`@tanstack/react-query`).

## Global Constraints

- **Pure frontend.** No backend, no new wire field, no change to poll cadences. The motion comes from the client tick.
- **Reuse the existing visibility hook.** `useNow` MUST compose `web/src/hooks/usePageVisible.ts` (`usePageVisible(): boolean`) — do NOT add a second `visibilitychange` listener (eliminating-parallel-implementations).
- **Purity rule (eslint-enforced):** `Date.now()` may be read ONLY inside an effect / interval callback, NEVER during render. Initial `now` state is `0` (a render-safe seed); the effect sets the real time after mount. This mirrors `CondensingProgress` (`Date.now()` only inside `useEffect`).
- **Live elapsed formula:** `displayElapsed = snapshotS + max(0, (nowMs − updatedAtMs) / 1000)`. `liveDeltaS = max(0, (nowMs − updatedAtMs) / 1000)`. The delta floors at 0 (handles `nowMs < updatedAt` and the initial `now=0` seed).
- **Overrun:** `isOverrun(liveDeltaS, remHighS) = remHighS != null && liveDeltaS >= remHighS`. On overrun the solid clamps to 100% (already handled by `bandSegments`) and the bar switches to the amber overdue color (`bg-amber-500`, the existing `checkStateColor('overdue')`) + a ⚠ marker.
- **Scope:** both the drill-down rows (`CheckRowList`) and the collapsed connector band (`StageTrack`) tick. Tick rate 1000 ms. `useNow` is called inside each ticking component (per the approved design; the per-instance timer cost is the spec's explicitly-accepted risk for the bounded number of open PRs).
- **Done/failed/non-running rows and no-range bars are unchanged** — they never tick (the existing `!done && duration != null` / range-present gates already exclude them).
- **Frontend gate (run from `web/`):** `npx vitest run`, `npx tsc -b --force` (NOT `tsc --noEmit` — it is false-green here), `npx eslint . --max-warnings 0`. The CI `frontend` check runs the FULL vitest suite, so a prop change can break sibling render tests — the full run is the real gate.

---

### Task 1: Pure tick helpers `liveElapsedS` + `isOverrun`

**Files:**
- Modify: `web/src/lib/etaBand.ts` (append after `pseudoElapsedFromPercent`, ~line 25)
- Test: `web/src/lib/__tests__/etaBand.test.ts` (append)

**Interfaces:**
- Produces:
  - `liveElapsedS(snapshotS: number, updatedAtMs: number, nowMs: number): number`
  - `isOverrun(liveDeltaS: number, remHighS: number | null): boolean`

- [ ] **Step 1: Write the failing tests**

```typescript
// append to web/src/lib/__tests__/etaBand.test.ts
import { liveElapsedS, isOverrun } from '@/lib/etaBand'

describe('liveElapsedS', () => {
  it('returns the snapshot when now == updatedAt (delta 0)', () => {
    expect(liveElapsedS(120, 1_000_000, 1_000_000)).toBe(120)
  })
  it('advances by whole seconds since the poll receive time', () => {
    expect(liveElapsedS(120, 1_000_000, 1_030_000)).toBeCloseTo(150, 5) // +30s
  })
  it('floors the delta at 0 when now < updatedAt', () => {
    expect(liveElapsedS(120, 1_000_000, 999_000)).toBe(120)
  })
})

describe('isOverrun', () => {
  it('is false when remHigh is null', () => {
    expect(isOverrun(999, null)).toBe(false)
  })
  it('is false while the delta is below remHigh', () => {
    expect(isOverrun(40, 60)).toBe(false)
  })
  it('is true once the delta reaches remHigh', () => {
    expect(isOverrun(60, 60)).toBe(true)
    expect(isOverrun(75, 60)).toBe(true)
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run (from `web/`): `npx vitest run src/lib/__tests__/etaBand.test.ts`
Expected: FAIL — `liveElapsedS`/`isOverrun` are not exported.

- [ ] **Step 3: Implement the helpers**

```typescript
// append to web/src/lib/etaBand.ts
// Live elapsed = the poll-time snapshot plus wall-clock since the browser received it.
// Delta floors at 0 (covers nowMs < updatedAtMs and the render-safe now=0 seed).
export function liveElapsedS(snapshotS: number, updatedAtMs: number, nowMs: number): number {
  return snapshotS + Math.max(0, (nowMs - updatedAtMs) / 1000)
}

// Overrun = the elapsed since the poll has consumed the entire p90 (high) remaining.
export function isOverrun(liveDeltaS: number, remHighS: number | null): boolean {
  return remHighS != null && liveDeltaS >= remHighS
}
```

- [ ] **Step 4: Run to verify it passes**

Run (from `web/`): `npx vitest run src/lib/__tests__/etaBand.test.ts`
Expected: PASS (existing `bandSegments`/`pseudoElapsedFromPercent` tests + the 6 new ones).

- [ ] **Step 5: Commit**

```bash
git add web/src/lib/etaBand.ts web/src/lib/__tests__/etaBand.test.ts
git commit -m "feat(pipeline): liveElapsedS + isOverrun tick helpers"
```

---

### Task 2: `useNow` hook (1s tick, paused on hidden tab)

**Files:**
- Create: `web/src/hooks/useNow.ts`
- Test: `web/src/hooks/useNow.test.tsx`

**Interfaces:**
- Consumes: `usePageVisible` from `web/src/hooks/usePageVisible.ts`.
- Produces: `useNow(intervalMs?: number): number` — current `Date.now()` ms, advanced on `intervalMs` (default 1000) while the tab is visible; frozen while hidden; `0` before the first post-mount tick.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/hooks/useNow.test.tsx
import { renderHook, act } from '@testing-library/react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { useNow } from './useNow'

function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { value: state, configurable: true })
  document.dispatchEvent(new Event('visibilitychange'))
}

describe('useNow', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    setVisibility('visible')
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('reports the current time after mount and advances on the interval', () => {
    vi.setSystemTime(1_000_000)
    const { result } = renderHook(() => useNow(1000))
    // effect ran on mount → now == system time
    expect(result.current).toBe(1_000_000)
    act(() => { vi.setSystemTime(1_002_000); vi.advanceTimersByTime(2000) })
    expect(result.current).toBe(1_002_000)
  })

  it('freezes while the tab is hidden and catches up when visible again', () => {
    vi.setSystemTime(2_000_000)
    const { result } = renderHook(() => useNow(1000))
    expect(result.current).toBe(2_000_000)
    act(() => { setVisibility('hidden') })
    act(() => { vi.setSystemTime(2_050_000); vi.advanceTimersByTime(50_000) })
    expect(result.current).toBe(2_000_000) // frozen — no tick while hidden
    act(() => { setVisibility('visible') })
    expect(result.current).toBe(2_050_000) // immediate catch-up on return
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run (from `web/`): `npx vitest run src/hooks/useNow.test.tsx`
Expected: FAIL — `./useNow` does not exist.

- [ ] **Step 3: Implement the hook**

```typescript
// web/src/hooks/useNow.ts
import { useEffect, useState } from 'react'
import { usePageVisible } from './usePageVisible'

/**
 * Current Date.now() ms, advanced every `intervalMs` while the tab is visible
 * and frozen while hidden (composes usePageVisible — no second visibility
 * listener). Returns 0 until the first post-mount tick so render stays pure
 * (no Date.now() during render).
 */
export function useNow(intervalMs = 1000): number {
  const visible = usePageVisible()
  const [now, setNow] = useState(0)
  useEffect(() => {
    if (!visible) return
    setNow(Date.now()) // immediate catch-up on mount / when the tab returns
    const id = setInterval(() => setNow(Date.now()), intervalMs)
    return () => clearInterval(id)
  }, [visible, intervalMs])
  return now
}
```

- [ ] **Step 4: Run to verify it passes**

Run (from `web/`): `npx vitest run src/hooks/useNow.test.tsx`
Expected: PASS (both cases).

- [ ] **Step 5: Commit**

```bash
git add web/src/hooks/useNow.ts web/src/hooks/useNow.test.tsx
git commit -m "feat(pipeline): useNow hook (1s tick, paused on hidden tab)"
```

---

### Task 3: `StageTrack` connector-band live tick + thread `updatedAt`

**Files:**
- Modify: `web/src/components/StageTrack.tsx`
- Modify: `web/src/components/PipelineView.tsx` (`PipelineView` reads `dataUpdatedAt`; `PipelineBoard` gains `updatedAt` prop and passes it to `StageTrack`)
- Test: `web/src/components/__tests__/StageTrack.test.tsx` (existing — update for the required prop + add tick assertions; if the existing test lives elsewhere, find it with `grep -rl StageTrack web/src/**/__tests__ web/src/**/*.test.tsx`)

**Interfaces:**
- Consumes: `useNow` (Task 2), `liveElapsedS`/`isOverrun` + `bandSegments`/`pseudoElapsedFromPercent` (Tasks 1 / existing), react-query `dataUpdatedAt` on `useAllPrs()`.
- Produces: `StageTrack` gains required prop `updatedAt: number`; `PipelineBoard` gains required prop `updatedAt: number`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/__tests__/StageTrack.test.tsx — add these cases (keep existing ones,
// updating their <StageTrack .../> usages to pass updatedAt={Date.now()} so they compile).
import { render } from '@testing-library/react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { StageTrack } from '@/components/StageTrack'

const CI_MODEL = { nodes: [{ id: 'ci', label: 'CI', kind: 'ci' }, { id: 'ready', label: 'Ready' }] }
const CI_ACTIVE = [{ id: 'ci', state: 'active' as const }, { id: 'ready', state: 'pending' as const }]

function solidWidth(container: HTMLElement): number {
  const band = container.querySelector('[data-testid="ci-band"]')
  const solid = band?.querySelector('span') as HTMLElement
  return parseFloat(solid.style.width) // "NN%" → NN
}

afterEach(() => vi.restoreAllMocks())

describe('StageTrack live tick', () => {
  it('advances the connector band solid width as now moves past the poll time', () => {
    const updatedAt = 1_000_000
    // snapshot render: now == updatedAt
    vi.spyOn(Date, 'now').mockReturnValue(updatedAt)
    const a = render(
      <StageTrack model={CI_MODEL} states={CI_ACTIVE} updatedAt={updatedAt}
        etaSeconds={300} etaLowSeconds={200} etaHighSeconds={600} etaPercent={50} />,
    )
    const w0 = solidWidth(a.container)
    a.unmount()
    // advanced render: now 120s later
    vi.spyOn(Date, 'now').mockReturnValue(updatedAt + 120_000)
    const b = render(
      <StageTrack model={CI_MODEL} states={CI_ACTIVE} updatedAt={updatedAt}
        etaSeconds={300} etaLowSeconds={200} etaHighSeconds={600} etaPercent={50} />,
    )
    expect(solidWidth(b.container)).toBeGreaterThan(w0)
  })

  it('switches the band to the amber overdue color on overrun', () => {
    const updatedAt = 1_000_000
    vi.spyOn(Date, 'now').mockReturnValue(updatedAt + 700_000) // 700s > etaHigh 600s
    const { container } = render(
      <StageTrack model={CI_MODEL} states={CI_ACTIVE} updatedAt={updatedAt}
        etaSeconds={300} etaLowSeconds={200} etaHighSeconds={600} etaPercent={50} />,
    )
    const solid = container.querySelector('[data-testid="ci-band"] span') as HTMLElement
    expect(solid.className).toContain('bg-amber-500')
  })
})
```

> **Note on mocking:** `useNow` calls `Date.now()` in its mount effect, which RTL flushes synchronously, so `vi.spyOn(Date,'now').mockReturnValue(...)` before `render` makes the first committed `now` deterministic. No fake timers needed for these component tests.

- [ ] **Step 2: Run to verify it fails**

Run (from `web/`): `npx vitest run src/components/__tests__/StageTrack.test.tsx`
Expected: FAIL — `StageTrack` has no `updatedAt` prop; band does not advance / has no overdue color.

- [ ] **Step 3: Implement the tick in `StageTrack`**

In `web/src/components/StageTrack.tsx`:
1. Add imports: `import { useNow } from '@/hooks/useNow'` and extend the etaBand import to `import { bandSegments, pseudoElapsedFromPercent, isOverrun } from '@/lib/etaBand'`.
2. Add `updatedAt: number` to `StageTrackProps` and destructure it in the signature.
3. At the top of the component body: `const now = useNow()`.
4. Replace the band block (the `showBand`/`seg`/render section, lines ~56-72) so the pseudo-elapsed advances and the color reflects overrun:

```tsx
              const liveDeltaS = Math.max(0, (now - updatedAt) / 1000)
              const showBand = prevNode?.kind === 'ci' && prev === 'active'
                && etaPercent != null && etaLowSeconds != null && etaHighSeconds != null
              const seg = showBand
                ? bandSegments({
                    elapsedS: pseudoElapsedFromPercent(etaSeconds ?? 0, etaPercent!) + liveDeltaS,
                    lowS: etaLowSeconds!, nominalS: etaSeconds ?? 0, highS: etaHighSeconds!,
                  })
                : null
              const bandColor = isOverrun(liveDeltaS, etaHighSeconds ?? null) ? 'bg-amber-500' : 'bg-accent'
              if (seg) {
                return (
                  <div data-testid="ci-band" className="relative h-1.5 w-10 shrink-0 overflow-hidden rounded bg-gray-200 dark:bg-gray-700">
                    <span className={cn('absolute inset-y-0 left-0 rounded-l', bandColor)} style={{ width: `${seg.solidPct}%` }} aria-hidden />
                    <span className={cn('absolute inset-y-0 opacity-30', bandColor)} style={{ left: `${seg.bandLeftPct}%`, width: `${seg.bandWidthPct}%` }} aria-hidden />
                    <span className="absolute inset-y-[-1px] w-px bg-foreground" style={{ left: `${seg.tickPct}%` }} aria-hidden />
                  </div>
                )
              }
              return <div className={cn('h-0.5 w-6 shrink-0', connectorClass(prev))} aria-hidden />
```

(`cn` is already imported in this file.)

- [ ] **Step 4: Thread `updatedAt` through `PipelineView` → `PipelineBoard` → `StageTrack`**

In `web/src/components/PipelineView.tsx`:
1. `PipelineBoard` props (the object type, ~lines 14-26): add `updatedAt: number` and destructure it.
2. The `<StageTrack ... />` render (~line 112): add `updatedAt={updatedAt}`.
3. `PipelineView` (~line 164): `const { data: prs = [], dataUpdatedAt } = useAllPrs()`.
4. The `<PipelineBoard ... />` render (~line 178): add `updatedAt={dataUpdatedAt}`.

- [ ] **Step 5: Run to verify it passes**

Run (from `web/`): `npx vitest run src/components/__tests__/StageTrack.test.tsx`
Expected: PASS. Then `npx tsc -b --force` — clean (all `StageTrack`/`PipelineBoard` call sites carry `updatedAt`).

- [ ] **Step 6: Commit**

```bash
git add web/src/components/StageTrack.tsx web/src/components/PipelineView.tsx web/src/components/__tests__/StageTrack.test.tsx
git commit -m "feat(pipeline): StageTrack connector band ticks live between polls"
```

---

### Task 4: `CheckRowList` drill-down rows live tick + thread `updatedAt`

**Files:**
- Modify: `web/src/components/CheckRowList.tsx`
- Modify: `web/src/components/CheckDrillDown.tsx` (gains `updatedAt` prop, passes to `CheckRowList`)
- Modify: `web/src/components/PipelineView.tsx` (`PipelineBoard` passes `updatedAt` to `<CheckDrillDown>`)
- Test: `web/src/components/__tests__/CheckRowList.test.tsx` (existing — update for the required prop + add tick assertions; find with `grep -rl CheckRowList web/src/**/__tests__ web/src/**/*.test.tsx` if the path differs)

**Interfaces:**
- Consumes: `useNow` (Task 2), `liveElapsedS`/`isOverrun` (Task 1), `dataUpdatedAt` threaded from Task 3.
- Produces: `CheckRowList` gains required prop `updatedAt: number`; `CheckDrillDown` gains required prop `updatedAt: number`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/__tests__/CheckRowList.test.tsx — add these cases (keep existing,
// updating their <CheckRowList .../> usages to pass updatedAt={Date.now()}).
import { render } from '@testing-library/react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { CheckRowList } from '@/components/CheckRowList'
import type { CheckRow } from '@/api/types'

const runningRow = (over = {}): CheckRow => ({
  name: 'build', workflow: 'ci', status: 'IN_PROGRESS', conclusion: 'pending',
  duration_seconds: 60, required: true, overdue: false,
  rem_low_seconds: 60, rem_nominal_seconds: 120, rem_high_seconds: 240, ...over,
})

function solidWidth(container: HTMLElement): number {
  const solid = container.querySelector('[data-band="solid"]') as HTMLElement
  return parseFloat(solid.style.width)
}

afterEach(() => vi.restoreAllMocks())

describe('CheckRowList live tick', () => {
  it('a running row advances its solid width as now passes the poll time', () => {
    const updatedAt = 1_000_000
    vi.spyOn(Date, 'now').mockReturnValue(updatedAt)
    const a = render(<CheckRowList checks={[runningRow()]} updatedAt={updatedAt} />)
    const w0 = solidWidth(a.container)
    a.unmount()
    vi.spyOn(Date, 'now').mockReturnValue(updatedAt + 90_000) // +90s
    const b = render(<CheckRowList checks={[runningRow()]} updatedAt={updatedAt} />)
    expect(solidWidth(b.container)).toBeGreaterThan(w0)
  })

  it('flips a row to overdue once the live delta passes rem_high_seconds', () => {
    const updatedAt = 1_000_000
    vi.spyOn(Date, 'now').mockReturnValue(updatedAt + 300_000) // 300s > rem_high 240s
    const { container } = render(<CheckRowList checks={[runningRow()]} updatedAt={updatedAt} />)
    expect(container.querySelector('[data-state="overdue"]')).not.toBeNull()
  })

  it('does not tick a done row (now has no effect)', () => {
    const done = runningRow({ status: 'COMPLETED', conclusion: 'success' })
    vi.spyOn(Date, 'now').mockReturnValue(2_000_000)
    const { container } = render(<CheckRowList checks={[done]} updatedAt={1_000_000} />)
    const solid = container.querySelector('[data-band="solid"]') as HTMLElement | null
    // done rows take the plain 100% bar, not a band → no live advance
    expect(solid).toBeNull()
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run (from `web/`): `npx vitest run src/components/__tests__/CheckRowList.test.tsx`
Expected: FAIL — `CheckRowList` has no `updatedAt` prop; rows don't advance/flip overdue.

- [ ] **Step 3: Implement the tick in `CheckRowList`**

Rewrite `web/src/components/CheckRowList.tsx` so a running row computes a live elapsed and a live state:

```tsx
// web/src/components/CheckRowList.tsx
import { cn } from '@/lib/utils'
import { checkStateColor, type CheckVisualState } from '@/lib/checkState'
import { formatEta } from '@/lib/formatEta'
import { bandSegments, liveElapsedS, isOverrun } from '@/lib/etaBand'
import { useNow } from '@/hooks/useNow'
import type { CheckRow } from '@/api/types'

function visualState(c: CheckRow): CheckVisualState {
  if (c.overdue) return 'overdue'
  if (c.conclusion === 'failure') return 'failed'
  if (c.conclusion === 'pending' || c.status === 'IN_PROGRESS' || c.status === 'QUEUED') return 'active'
  if (c.conclusion === 'success') return 'done'
  return 'pending'
}

export function CheckRowList({ checks, updatedAt }: { checks: CheckRow[]; updatedAt: number }) {
  const now = useNow()
  const sorted = [...checks].sort((a, b) => {
    const wa = a.workflow ?? ''
    const wb = b.workflow ?? ''
    return wa < wb ? -1 : wa > wb ? 1 : a.name < b.name ? -1 : a.name > b.name ? 1 : 0
  })
  return (
    <ul className="space-y-0.5">
      {sorted.map((c) => {
        const baseState = visualState(c)
        const done = baseState === 'done' || baseState === 'failed'
        const running = !done && c.duration_seconds != null
        const liveDeltaS = Math.max(0, (now - updatedAt) / 1000)
        const overrun = running && isOverrun(liveDeltaS, c.rem_high_seconds ?? null)
        const state: CheckVisualState = overrun ? 'overdue' : baseState
        const displayElapsed = running ? liveElapsedS(c.duration_seconds as number, updatedAt, now) : c.duration_seconds
        const seg = running
          ? bandSegments({
              elapsedS: displayElapsed as number,
              lowS: c.rem_low_seconds ?? NaN,
              nominalS: c.rem_nominal_seconds ?? NaN,
              highS: c.rem_high_seconds ?? NaN,
            })
          : null
        return (
          <li
            key={`${c.workflow ?? ''}/${c.name}`}
            data-testid={`check-row-${c.name}`}
            data-state={state}
            className="flex items-center gap-2 text-xs"
          >
            <span className="w-48 shrink-0 truncate text-muted-foreground">{c.name}</span>
            <span className="relative h-1.5 flex-1 overflow-hidden rounded bg-gray-200 dark:bg-gray-700">
              {seg ? (
                <>
                  <span data-band="solid" className={cn('absolute inset-y-0 left-0 rounded-l', checkStateColor(state))}
                    style={{ width: `${seg.solidPct}%` }} aria-hidden />
                  <span data-band="range" className={cn('absolute inset-y-0 opacity-30', checkStateColor(state))}
                    style={{ left: `${seg.bandLeftPct}%`, width: `${seg.bandWidthPct}%` }} aria-hidden />
                  <span data-band="tick" className="absolute inset-y-[-1px] w-px bg-foreground"
                    style={{ left: `${seg.tickPct}%` }} aria-hidden />
                </>
              ) : (
                <span className={cn('absolute inset-y-0 left-0 rounded', checkStateColor(state))}
                  style={{ width: done ? '100%' : '40%' }} aria-hidden />
              )}
            </span>
            <span className="w-16 shrink-0 text-right tabular-nums text-muted-foreground">
              {displayElapsed != null ? formatEta(displayElapsed) : '—'}
              {(overrun || c.overdue) ? ' ⚠' : ''}
            </span>
          </li>
        )
      })}
    </ul>
  )
}
```

- [ ] **Step 4: Thread `updatedAt` through `CheckDrillDown` and `PipelineBoard`**

In `web/src/components/CheckDrillDown.tsx`:
```tsx
export function CheckDrillDown({ groups, updatedAt }: { groups: CheckGroup[]; updatedAt: number }) {
```
and the render: `<CheckRowList checks={grp.checks} updatedAt={updatedAt} />`.

In `web/src/components/PipelineView.tsx`, the `<CheckDrillDown groups={p.check_groups} />` render (~line 148): add `updatedAt={updatedAt}` (the prop already exists on `PipelineBoard` from Task 3).

- [ ] **Step 5: Run to verify it passes**

Run (from `web/`): `npx vitest run src/components/__tests__/CheckRowList.test.tsx`
Expected: PASS. Then `npx tsc -b --force` — clean.

- [ ] **Step 6: Commit**

```bash
git add web/src/components/CheckRowList.tsx web/src/components/CheckDrillDown.tsx web/src/components/PipelineView.tsx web/src/components/__tests__/CheckRowList.test.tsx
git commit -m "feat(pipeline): CheckRowList drill-down rows tick live between polls"
```

---

### Task 5: Full frontend gate + sibling-test fallout

**Files:**
- Modify: any test that renders `StageTrack`, `CheckRowList`, `CheckDrillDown`, `PipelineBoard`, or `PipelineView` and now fails to compile/pass because of the new required `updatedAt` prop (e.g. existing `PipelineView`/`Dashboard`/board integration tests). Fix only the prop wiring — no behavior change.

**Interfaces:**
- Consumes: everything from Tasks 1–4. Produces: a green full suite.

- [ ] **Step 1: Run the full type + lint gate**

Run (from `web/`):
```bash
npx tsc -b --force
npx eslint . --max-warnings 0
```
Expected: any failures are missing-`updatedAt` props at render sites, or a `react-hooks/*` purity warning. List them.

- [ ] **Step 2: Run the full vitest suite**

Run (from `web/`): `npx vitest run`
Expected: failures (if any) are sibling tests that render the changed components without `updatedAt`, or snapshot/width assertions that assumed the old static bar.

- [ ] **Step 3: Fix the fallout**

For each failing render site, pass `updatedAt={Date.now()}` (tests) or thread the real `dataUpdatedAt` (app code). Do NOT weaken any existing behavioral assertion; if a test asserted a static width that now ticks, make its `Date.now()` deterministic (`vi.spyOn(Date,'now').mockReturnValue(updatedAt)`) so the snapshot render still matches. There must be no new `react-hooks/exhaustive-deps` or purity warnings (the eslint gate is `--max-warnings 0`).

- [ ] **Step 4: Re-run the full gate to confirm green**

Run (from `web/`):
```bash
npx vitest run
npx tsc -b --force
npx eslint . --max-warnings 0
```
Expected: all three clean.

- [ ] **Step 5: Commit**

```bash
git add -A web/
git commit -m "test(pipeline): wire updatedAt through pipeline render-site tests"
```

---

## Self-Review

**Spec coverage:**
- `useNow` hook, 1s, pause on hidden (compose existing visibility hook) → Task 2. ✅
- `liveElapsedS` + `isOverrun` pure helpers → Task 1. ✅
- `useAllPrs` exposes `dataUpdatedAt`; `PipelineView` threads `updatedAt` → Tasks 3 (board+StageTrack) + 4 (CheckDrillDown). ✅
- `CheckRowList` per-row tick (displayElapsed → bandSegments + label + liveState; done rows unchanged) → Task 4. ✅
- `StageTrack` connector-band tick (pseudoElapsed + liveDelta; overrun → amber) → Task 3. ✅
- Overrun → pin full (bandSegments clamps) + amber + ⚠ → Tasks 3 (band color) + 4 (overdue state + ⚠). ✅
- Edge cases: `nowMs<updatedAt` floored (Task 1 test); hidden-tab freeze+catch-up (Task 2 test); done mid-tick stops (Task 4 done-row test); missing range falls back to plain bar (unchanged seg gate). ✅
- Purity (no `Date.now()` in render) → `useNow` reads time only in the effect; `now=0` seed → Task 2 + eslint gate in Task 5. ✅
- Full vitest + `tsc -b --force` + eslint `--max-warnings 0` → Task 5. ✅

**Placeholder scan:** No TBD/TODO. Every code step shows the full code. Test render-site fix-ups (Task 5) are described concretely (pass `updatedAt`, make `Date.now` deterministic) rather than left vague because the exact failing set is environment-discovered — Step 1/2 list them before Step 3 fixes them.

**Type consistency:** `updatedAt: number` is the prop name on `StageTrack`, `PipelineBoard`, `CheckDrillDown`, `CheckRowList`; `dataUpdatedAt` (react-query) is its source in `PipelineView`. `liveElapsedS(snapshotS, updatedAtMs, nowMs)` and `isOverrun(liveDeltaS, remHighS|null)` are used with those exact signatures in Tasks 3–4. `useNow(intervalMs=1000): number` called arg-less in both leaves. `liveDeltaS = Math.max(0,(now-updatedAt)/1000)` computed identically in both components and in `isOverrun`'s contract.
