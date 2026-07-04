# Plotroom Progress Bars Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ccswitch's self-normalizing CI progress bars with plotroom's shared-scale Gantt method, so bar length encodes duration across a whole check panel.

**Architecture:** Add shared-scale helpers to `etaBand.ts`, extend the state→color map in `checkState.ts` with `skipped`/`queued`/texture, extract a single `GanttBar` primitive that replaces the duplicated inline bar markup, and re-point `CheckRowList` (shared scale per panel) and `StageTrack` (single-bar) at it.

**Tech Stack:** React + TypeScript + Vite + Tailwind, Vitest + Testing Library. Frontend only — no backend, no API-type changes.

## Global Constraints

- Work only under `web/`. Run all gates from `web/`: `npx vitest run`, `npx tsc --noEmit`, `npx eslint .`.
- Preserve existing DOM contract used by tests: `data-band="solid|range|tick"`, `data-testid="check-row-<name>"` with `data-state`, and `data-testid="ci-band"`.
- Reuse existing helpers (`liveElapsedS`, `isOverrun`, `formatEta`, `useNow`, `cn`). Do not add parallel copies (CLAUDE.md single-source-of-truth).
- `conclusion` reaches the client lowercased (`success`/`failure`/`pending`/`skipped`); `status` is `QUEUED`/`IN_PROGRESS`/`COMPLETED`.
- Queued jobs render a fixed **15%** fill stub. `runner-wait`/flake/blocked-on are OUT OF SCOPE (no backend fields) — tracked in a follow-up issue.

---

### Task 1: Shared-scale helpers in `etaBand.ts`

**Files:**
- Modify: `web/src/lib/etaBand.ts`
- Test: `web/src/lib/__tests__/etaBand.test.ts`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `interface GanttRow { elapsed: number; expLow: number | null; expNominal: number | null; expHigh: number | null }`
  - `interface GanttSegments { fillPct: number; bandLeftPct: number | null; bandWidthPct: number | null; tickPct: number | null }`
  - `expectedTotals(row: { duration_seconds: number | null; rem_low_seconds?: number | null; rem_nominal_seconds?: number | null; rem_high_seconds?: number | null }): { expLow: number | null; expNominal: number | null; expHigh: number | null }`
  - `ganttScale(rows: GanttRow[]): number`
  - `ganttSegments(row: GanttRow, scale: number, queued?: boolean): GanttSegments`

- [ ] **Step 1: Write the failing tests** — append to `web/src/lib/__tests__/etaBand.test.ts`:

```ts
import { expectedTotals, ganttScale, ganttSegments } from '../etaBand'

describe('expectedTotals', () => {
  it('adds remaining estimates onto the elapsed snapshot', () => {
    expect(expectedTotals({ duration_seconds: 60, rem_low_seconds: 10, rem_nominal_seconds: 30, rem_high_seconds: 90 }))
      .toEqual({ expLow: 70, expNominal: 90, expHigh: 150 })
  })
  it('returns null for absent remaining fields and treats null duration as 0', () => {
    expect(expectedTotals({ duration_seconds: null })).toEqual({ expLow: null, expNominal: null, expHigh: null })
    expect(expectedTotals({ duration_seconds: null, rem_high_seconds: 45 }).expHigh).toBe(45)
  })
})

describe('ganttScale', () => {
  it('is the max of elapsed and expHigh across all rows', () => {
    const rows = [
      { elapsed: 26, expLow: null, expNominal: null, expHigh: null },
      { elapsed: 40, expLow: 60, expNominal: 120, expHigh: 180 },
    ]
    expect(ganttScale(rows)).toBe(180)
  })
  it('floors at 60 when everything is zero', () => {
    expect(ganttScale([{ elapsed: 0, expLow: null, expNominal: null, expHigh: null }])).toBe(60)
  })
})

describe('ganttSegments', () => {
  it('normalizes fill against the shared scale so a 26s bar is ~0.14x a 180s scale', () => {
    const seg = ganttSegments({ elapsed: 26, expLow: null, expNominal: null, expHigh: null }, 180)
    expect(seg.fillPct).toBeCloseTo((26 / 180) * 100, 4)
    expect(seg.bandLeftPct).toBeNull()
    expect(seg.tickPct).toBeNull()
  })
  it('computes band + tick positions on the shared scale', () => {
    const seg = ganttSegments({ elapsed: 40, expLow: 60, expNominal: 120, expHigh: 180 }, 180)
    expect(seg.bandLeftPct).toBeCloseTo((60 / 180) * 100, 4)
    expect(seg.bandWidthPct).toBeCloseTo(((180 - 60) / 180) * 100, 4)
    expect(seg.tickPct).toBeCloseTo((120 / 180) * 100, 4)
  })
  it('forces a 15% stub for queued rows regardless of elapsed', () => {
    expect(ganttSegments({ elapsed: 0, expLow: null, expNominal: null, expHigh: null }, 180, true).fillPct).toBe(15)
  })
  it('clamps fill and band to 100%', () => {
    const seg = ganttSegments({ elapsed: 500, expLow: 400, expNominal: 450, expHigh: 500 }, 180)
    expect(seg.fillPct).toBe(100)
    expect(seg.bandLeftPct).toBe(100)
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd web && npx vitest run src/lib/__tests__/etaBand.test.ts`
Expected: FAIL — `expectedTotals`/`ganttScale`/`ganttSegments` are not exported.

- [ ] **Step 3: Implement** — append to `web/src/lib/etaBand.ts` (keep existing exports intact):

```ts
export interface GanttRow { elapsed: number; expLow: number | null; expNominal: number | null; expHigh: number | null }
export interface GanttSegments { fillPct: number; bandLeftPct: number | null; bandWidthPct: number | null; tickPct: number | null }

// ccswitch stores REMAINING estimates; plotroom uses TOTAL expected. total = snapshot elapsed + remaining.
export function expectedTotals(row: {
  duration_seconds: number | null
  rem_low_seconds?: number | null
  rem_nominal_seconds?: number | null
  rem_high_seconds?: number | null
}): { expLow: number | null; expNominal: number | null; expHigh: number | null } {
  const base = row.duration_seconds ?? 0
  const add = (rem: number | null | undefined) => (rem == null ? null : base + rem)
  return { expLow: add(row.rem_low_seconds), expNominal: add(row.rem_nominal_seconds), expHigh: add(row.rem_high_seconds) }
}

// One shared time scale per panel: the longest elapsed-or-p90-expected across all rows (floor 60s).
export function ganttScale(rows: GanttRow[]): number {
  let max = 0
  for (const r of rows) max = Math.max(max, r.elapsed, r.expHigh ?? 0, r.expNominal ?? 0)
  return max > 0 ? max : 60
}

const pctOf = (v: number, scale: number) => Math.min(100, (v / scale) * 100)

export function ganttSegments(row: GanttRow, scale: number, queued = false): GanttSegments {
  const s = scale > 0 ? scale : 60
  const fillPct = queued ? 15 : pctOf(row.elapsed, s)
  let bandLeftPct: number | null = null
  let bandWidthPct: number | null = null
  if (row.expLow != null && row.expHigh != null) {
    bandLeftPct = pctOf(row.expLow, s)
    bandWidthPct = Math.max(0, pctOf(row.expHigh, s) - bandLeftPct)
  }
  const tickPct = row.expNominal != null ? pctOf(row.expNominal, s) : null
  return { fillPct, bandLeftPct, bandWidthPct, tickPct }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd web && npx vitest run src/lib/__tests__/etaBand.test.ts`
Expected: PASS (all, including pre-existing cases).

- [ ] **Step 5: Commit**

```bash
git add web/src/lib/etaBand.ts web/src/lib/__tests__/etaBand.test.ts
git commit -m "feat(pipeline): shared-scale gantt helpers in etaBand

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `skipped`/`queued` states + textures in `checkState.ts`

**Files:**
- Modify: `web/src/lib/checkState.ts`
- Test: `web/src/lib/__tests__/checkState.test.ts`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `type CheckVisualState = 'done' | 'active' | 'failed' | 'pending' | 'overdue' | 'skipped' | 'queued'`
  - `checkStateColor(state: CheckVisualState): string` (extended)
  - `checkStateTexture(state: CheckVisualState): string` — returns a CSS `background-image` value (or `''`).

- [ ] **Step 1: Write the failing tests** — append to `web/src/lib/__tests__/checkState.test.ts`:

```ts
import { checkStateColor, checkStateTexture } from '../checkState'

describe('checkStateColor (new states)', () => {
  it('maps skipped and queued to muted gray', () => {
    expect(checkStateColor('skipped')).toContain('gray')
    expect(checkStateColor('queued')).toContain('gray')
  })
})

describe('checkStateTexture', () => {
  it('gives overdue a 90deg white stripe overlay', () => {
    expect(checkStateTexture('overdue')).toMatch(/repeating-linear-gradient\(90deg/)
  })
  it('gives failed a -45deg white stripe overlay', () => {
    expect(checkStateTexture('failed')).toMatch(/repeating-linear-gradient\(-45deg/)
  })
  it('returns empty string for plain states', () => {
    expect(checkStateTexture('done')).toBe('')
    expect(checkStateTexture('active')).toBe('')
    expect(checkStateTexture('queued')).toBe('')
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd web && npx vitest run src/lib/__tests__/checkState.test.ts`
Expected: FAIL — `checkStateTexture` not exported; `'skipped'`/`'queued'` not assignable.

- [ ] **Step 3: Implement** — in `web/src/lib/checkState.ts`, widen the type, extend the color switch, add the texture fn:

```ts
export type CheckVisualState = 'done' | 'active' | 'failed' | 'pending' | 'overdue' | 'skipped' | 'queued'

export function checkStateColor(state: CheckVisualState): string {
  switch (state) {
    case 'done': return 'bg-green-500'
    case 'active': return 'bg-accent'
    case 'failed': return 'bg-red-500'
    case 'overdue': return 'bg-amber-500'
    case 'skipped':
    case 'queued':
    case 'pending':
    default: return 'bg-gray-300 dark:bg-gray-600'
  }
}

// CVD-safe overlay applied on top of the fill color (see GanttBar). Plotroom values.
export function checkStateTexture(state: CheckVisualState): string {
  switch (state) {
    case 'overdue':
      return 'repeating-linear-gradient(90deg, transparent 0 4px, rgba(255,255,255,0.24) 4px 6px)'
    case 'failed':
      return 'repeating-linear-gradient(-45deg, rgba(255,255,255,0.30) 0 2px, transparent 2px 6px)'
    default:
      return ''
  }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd web && npx vitest run src/lib/__tests__/checkState.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/lib/checkState.ts web/src/lib/__tests__/checkState.test.ts
git commit -m "feat(pipeline): skipped/queued states + CVD-safe textures

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `GanttBar` shared primitive

**Files:**
- Create: `web/src/components/GanttBar.tsx`
- Test: `web/src/components/__tests__/GanttBar.test.tsx`

**Interfaces:**
- Consumes: `cn` from `@/lib/utils`.
- Produces:
  ```ts
  interface GanttBarProps {
    fillPct: number
    colorClass: string
    bandLeftPct?: number | null
    bandWidthPct?: number | null
    tickPct?: number | null
    textureImage?: string
    ariaNow?: number
    testId?: string
    className?: string   // track sizing; default 'h-1.5 flex-1'
  }
  function GanttBar(props: GanttBarProps): JSX.Element
  ```

- [ ] **Step 1: Write the failing test** — `web/src/components/__tests__/GanttBar.test.tsx`:

```tsx
import { render } from '@testing-library/react'
import { GanttBar } from '../GanttBar'

describe('GanttBar', () => {
  it('renders a solid fill at the given width with progressbar role', () => {
    const { container, getByRole } = render(<GanttBar fillPct={42} colorClass="bg-accent" ariaNow={42} />)
    const solid = container.querySelector('[data-band="solid"]') as HTMLElement
    expect(solid.style.width).toBe('42%')
    expect(getByRole('progressbar')).toHaveAttribute('aria-valuenow', '42')
  })
  it('renders band and tick only when provided', () => {
    const { container } = render(
      <GanttBar fillPct={20} colorClass="bg-accent" bandLeftPct={30} bandWidthPct={40} tickPct={55} />,
    )
    expect(container.querySelector('[data-band="range"]')).toBeInTheDocument()
    expect(container.querySelector('[data-band="tick"]')).toBeInTheDocument()
  })
  it('omits band when width is zero and tick when null', () => {
    const { container } = render(
      <GanttBar fillPct={20} colorClass="bg-accent" bandLeftPct={30} bandWidthPct={0} tickPct={null} />,
    )
    expect(container.querySelector('[data-band="range"]')).toBeNull()
    expect(container.querySelector('[data-band="tick"]')).toBeNull()
  })
  it('applies a texture overlay when textureImage is set', () => {
    const { container } = render(
      <GanttBar fillPct={50} colorClass="bg-red-500" textureImage="repeating-linear-gradient(-45deg, red 0 2px, transparent 2px 6px)" />,
    )
    const overlay = container.querySelector('[data-band="texture"]') as HTMLElement
    expect(overlay.style.backgroundImage).toContain('repeating-linear-gradient')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run src/components/__tests__/GanttBar.test.tsx`
Expected: FAIL — module `../GanttBar` not found.

- [ ] **Step 3: Implement** — `web/src/components/GanttBar.tsx`:

```tsx
import { cn } from '@/lib/utils'

export interface GanttBarProps {
  fillPct: number
  colorClass: string
  bandLeftPct?: number | null
  bandWidthPct?: number | null
  tickPct?: number | null
  textureImage?: string
  ariaNow?: number
  testId?: string
  className?: string
}

// Single shared bar primitive: track + optional p10–p90 band + fill (with optional
// CVD-safe texture overlay) + optional nominal tick. Used by CheckRowList and StageTrack.
export function GanttBar({
  fillPct, colorClass, bandLeftPct, bandWidthPct, tickPct, textureImage, ariaNow, testId,
  className = 'h-1.5 flex-1',
}: GanttBarProps) {
  return (
    <span
      data-testid={testId}
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(ariaNow ?? fillPct)}
      className={cn('relative block overflow-hidden rounded bg-gray-200 dark:bg-gray-700', className)}
    >
      {bandLeftPct != null && bandWidthPct != null && bandWidthPct > 0 && (
        <span data-band="range" className={cn('absolute inset-y-0 opacity-30', colorClass)}
          style={{ left: `${bandLeftPct}%`, width: `${bandWidthPct}%` }} aria-hidden />
      )}
      <span data-band="solid" className={cn('absolute inset-y-0 left-0 rounded-l', colorClass)}
        style={{ width: `${fillPct}%` }} aria-hidden>
        {textureImage ? (
          <span data-band="texture" className="absolute inset-0" style={{ backgroundImage: textureImage }} aria-hidden />
        ) : null}
      </span>
      {tickPct != null && (
        <span data-band="tick" className="absolute inset-y-[-1px] w-px bg-foreground"
          style={{ left: `${tickPct}%` }} aria-hidden />
      )}
    </span>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx vitest run src/components/__tests__/GanttBar.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/GanttBar.tsx web/src/components/__tests__/GanttBar.test.tsx
git commit -m "feat(pipeline): GanttBar shared bar primitive

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Rewrite `CheckRowList` on the shared scale

**Files:**
- Modify: `web/src/components/CheckRowList.tsx`
- Test: `web/src/components/__tests__/CheckRowList.test.tsx` (update expectations changed by shared-scale)

**Interfaces:**
- Consumes: `expectedTotals`, `ganttScale`, `ganttSegments`, `GanttRow` (Task 1); `checkStateColor`, `checkStateTexture`, `CheckVisualState` (Task 2); `GanttBar` (Task 3); existing `liveElapsedS`, `isOverrun`, `formatEta`, `useNow`.
- Produces: nothing consumed downstream.

- [ ] **Step 1: Update/write the tests.** Replace the body of `web/src/components/__tests__/CheckRowList.test.tsx` with expectations for the shared scale (helper reads the solid width; done rows are now proportional, not 100%):

```tsx
import { render, screen } from '@testing-library/react'
import { CheckRowList } from '../CheckRowList'
import type { CheckRow } from '@/api/types'

function row(p: Partial<CheckRow> & { name: string }): CheckRow {
  return {
    name: p.name, workflow: p.workflow ?? 'w', status: p.status ?? 'COMPLETED',
    conclusion: p.conclusion ?? 'success', duration_seconds: p.duration_seconds ?? null,
    required: true, overdue: p.overdue ?? false,
    rem_low_seconds: p.rem_low_seconds ?? null, rem_nominal_seconds: p.rem_nominal_seconds ?? null,
    rem_high_seconds: p.rem_high_seconds ?? null,
  }
}
const solidWidth = (testid: string) => {
  const el = screen.getByTestId(testid).querySelector('[data-band="solid"]') as HTMLElement
  return parseFloat(el.style.width)
}

it('sets data-state and label per conclusion', () => {
  render(<CheckRowList updatedAt={0} checks={[
    row({ name: 'lint', conclusion: 'success', duration_seconds: 300 }),
    row({ name: 'test', conclusion: 'failure', duration_seconds: 120 }),
    row({ name: 'skip', conclusion: 'skipped', duration_seconds: null }),
  ]} />)
  expect(screen.getByTestId('check-row-lint')).toHaveAttribute('data-state', 'done')
  expect(screen.getByTestId('check-row-lint')).toHaveTextContent('5m ✓')
  expect(screen.getByTestId('check-row-test')).toHaveAttribute('data-state', 'failed')
  expect(screen.getByTestId('check-row-skip')).toHaveTextContent('–')
})

it('shares one scale so a 30s bar is ~0.1x a 300s bar', () => {
  render(<CheckRowList updatedAt={0} checks={[
    row({ name: 'short', conclusion: 'success', duration_seconds: 30 }),
    row({ name: 'long', conclusion: 'success', duration_seconds: 300 }),
  ]} />)
  expect(solidWidth('check-row-long')).toBe(100)
  expect(solidWidth('check-row-short')).toBeCloseTo(10, 0)
})

it('renders queued rows as a 15% stub', () => {
  render(<CheckRowList updatedAt={0} checks={[
    row({ name: 'q', status: 'QUEUED', conclusion: 'pending', duration_seconds: null }),
    row({ name: 'done', conclusion: 'success', duration_seconds: 300 }),
  ]} />)
  expect(screen.getByTestId('check-row-q')).toHaveAttribute('data-state', 'queued')
  expect(solidWidth('check-row-q')).toBe(15)
})

it('a running row shows band + tick and an elapsed/expected label', () => {
  render(<CheckRowList updatedAt={0} checks={[
    row({ name: 'run', status: 'IN_PROGRESS', conclusion: 'pending', duration_seconds: 60,
      rem_low_seconds: 30, rem_nominal_seconds: 60, rem_high_seconds: 120 }),
  ]} />)
  const r = screen.getByTestId('check-row-run')
  expect(r.querySelector('[data-band="range"]')).toBeInTheDocument()
  expect(r.querySelector('[data-band="tick"]')).toBeInTheDocument()
  expect(r).toHaveTextContent('/ ~2m')
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd web && npx vitest run src/components/__tests__/CheckRowList.test.tsx`
Expected: FAIL — `skip`/`queued` states and shared-scale widths not yet implemented.

- [ ] **Step 3: Implement** — replace `web/src/components/CheckRowList.tsx` with:

```tsx
// web/src/components/CheckRowList.tsx
import { GanttBar } from '@/components/GanttBar'
import { checkStateColor, checkStateTexture, type CheckVisualState } from '@/lib/checkState'
import { formatEta } from '@/lib/formatEta'
import { expectedTotals, ganttScale, ganttSegments, liveElapsedS, isOverrun, type GanttRow } from '@/lib/etaBand'
import { useNow } from '@/hooks/useNow'
import type { CheckRow } from '@/api/types'

function baseState(c: CheckRow): CheckVisualState {
  if (c.overdue) return 'overdue'
  if (c.conclusion === 'failure') return 'failed'
  if (c.conclusion === 'success') return 'done'
  if (c.conclusion === 'skipped') return 'skipped'
  if (c.conclusion === 'pending' || c.status === 'IN_PROGRESS') return 'active'
  if (c.status === 'QUEUED') return 'queued'
  return 'pending'
}

function timeText(state: CheckVisualState, elapsedS: number, expNominalS: number | null): string {
  const e = formatEta(elapsedS)
  switch (state) {
    case 'done': return `${e} ✓`
    case 'failed': return `${e} ✗`
    case 'overdue': return `${e} ⚠ overdue`
    case 'skipped': return '–'
    case 'queued': return '—'
    case 'active': return expNominalS != null ? `${e} / ~${formatEta(expNominalS)}` : e
    default: return e
  }
}

export function CheckRowList({ checks, updatedAt }: { checks: CheckRow[]; updatedAt: number }) {
  const now = useNow()
  const sorted = [...checks].sort((a, b) => {
    const wa = a.workflow ?? '', wb = b.workflow ?? ''
    return wa < wb ? -1 : wa > wb ? 1 : a.name < b.name ? -1 : a.name > b.name ? 1 : 0
  })

  // Derive per-row live elapsed + expected totals, resolve state, then one shared scale for the panel.
  const derived = sorted.map((c) => {
    const base = baseState(c)
    const running = base === 'active'
    const liveDeltaS = Math.max(0, (now - updatedAt) / 1000)
    const overrun = running && isOverrun(liveDeltaS, c.rem_high_seconds ?? null)
    const state: CheckVisualState = overrun ? 'overdue' : base
    const elapsed = running ? liveElapsedS(c.duration_seconds ?? 0, updatedAt, now) : (c.duration_seconds ?? 0)
    const totals = expectedTotals(c)
    const gantt: GanttRow = { elapsed, expLow: totals.expLow, expNominal: totals.expNominal, expHigh: totals.expHigh }
    return { c, state, elapsed, gantt, queued: state === 'queued', expNominal: totals.expNominal }
  })
  const scale = ganttScale(derived.map((d) => d.gantt))

  return (
    <ul className="space-y-0.5">
      {derived.map(({ c, state, elapsed, gantt, queued, expNominal }) => {
        const seg = ganttSegments(gantt, scale, queued)
        return (
          <li
            key={`${c.workflow ?? ''}/${c.name}`}
            data-testid={`check-row-${c.name}`}
            data-state={state}
            className="flex items-center gap-2 text-xs"
          >
            <span className="w-48 shrink-0 truncate text-muted-foreground">{c.name}</span>
            <GanttBar
              fillPct={seg.fillPct}
              colorClass={checkStateColor(state)}
              bandLeftPct={seg.bandLeftPct}
              bandWidthPct={seg.bandWidthPct}
              tickPct={seg.tickPct}
              textureImage={checkStateTexture(state)}
              ariaNow={seg.fillPct}
            />
            <span className="w-24 shrink-0 text-right tabular-nums text-muted-foreground">
              {timeText(state, elapsed, expNominal)}
            </span>
          </li>
        )
      })}
    </ul>
  )
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd web && npx vitest run src/components/__tests__/CheckRowList.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/CheckRowList.tsx web/src/components/__tests__/CheckRowList.test.tsx
git commit -m "feat(pipeline): CheckRowList on shared-scale gantt bars

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Route `StageTrack` band through `GanttBar`

**Files:**
- Modify: `web/src/components/StageTrack.tsx`
- Test: `web/src/components/__tests__/StageTrack.test.tsx` (keep `ci-band` assertions; the inner solid is now inside GanttBar)

**Interfaces:**
- Consumes: `GanttBar` (Task 3), `checkStateTexture` (Task 2); existing `bandSegments`, `pseudoElapsedFromPercent`, `isOverrun`, `useNow`.
- Produces: nothing downstream.

- [ ] **Step 1: Confirm the existing test still targets `ci-band`.** The test reads `container.querySelector('[data-testid="ci-band"] span')` for the solid — GanttBar renders `[data-band="solid"]` as a `span` inside. Update that selector to `[data-testid="ci-band"] [data-band="solid"]` in `StageTrack.test.tsx` (line ~129) so it is unambiguous:

```tsx
const solid = container.querySelector('[data-testid="ci-band"] [data-band="solid"]') as HTMLElement
```

- [ ] **Step 2: Run test to verify current state**

Run: `cd web && npx vitest run src/components/__tests__/StageTrack.test.tsx`
Expected: PASS currently (inline markup) — this confirms the selector before the refactor. (If it fails on the selector edit, that is Step 1 taking effect; proceed.)

- [ ] **Step 3: Implement** — in `web/src/components/StageTrack.tsx`, replace the inline band `<div data-testid="ci-band">…three spans…</div>` (the `if (seg) { return (…) }` block) with `GanttBar`, and add the imports. New imports at top:

```tsx
import { GanttBar } from '@/components/GanttBar'
import { checkStateTexture } from '@/lib/checkState'
```

Replace the `if (seg) { return ( … ) }` block with:

```tsx
if (seg) {
  const fillPct = overrun ? 100 : seg.solidPct
  return (
    <GanttBar
      testId="ci-band"
      className="h-1.5 w-10 shrink-0"
      fillPct={fillPct}
      colorClass={bandColor}
      bandLeftPct={seg.bandLeftPct}
      bandWidthPct={seg.bandWidthPct}
      tickPct={seg.tickPct}
      textureImage={overrun ? checkStateTexture('overdue') : ''}
      ariaNow={fillPct}
    />
  )
}
```

(`bandColor` and `overrun` are already computed just above in the existing code.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd web && npx vitest run src/components/__tests__/StageTrack.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/StageTrack.tsx web/src/components/__tests__/StageTrack.test.tsx
git commit -m "refactor(pipeline): StageTrack band via GanttBar primitive

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Remove dead code, full gates, PR

**Files:**
- Modify: `web/src/lib/etaBand.ts` (remove `bandSegments` only if no remaining consumers)

- [ ] **Step 1: Check for remaining `bandSegments` consumers**

Run: `cd web && grep -rn "bandSegments" src/ | grep -v "__tests__"`
- If StageTrack still uses `bandSegments` (it does — it drives the single-bar seg), KEEP it. Do NOT remove exports that are still imported. Only delete a helper that has zero non-test consumers (no half-dead code — CLAUDE.md).

- [ ] **Step 2: Full frontend gates**

Run: `cd web && npx vitest run && npx tsc --noEmit && npx eslint .`
Expected: all green. Fix any type/lint fallout (e.g. unused imports left in `CheckRowList.tsx`).

- [ ] **Step 3: Push and open PR with auto-merge** (per CLAUDE.md completion workflow)

```bash
git push -u origin feat/plotroom-gantt-progress-bars
gh pr create --title "feat(pipeline): plotroom-style shared-scale progress bars" \
  --body "$(cat <<'EOF'
Replaces the self-normalizing CI progress bars with plotroom's shared-scale Gantt method: all rows in a check panel share one time scale, so bar length encodes duration and the critical path is visible at a glance.

- New `GanttBar` primitive replaces duplicated inline bar markup (CheckRowList + StageTrack).
- `etaBand.ts`: `expectedTotals`, `ganttScale`, `ganttSegments`.
- `checkState.ts`: `skipped`/`queued` states + CVD-safe overdue/failed textures.
- Queued jobs render a 15% stub; labels match plotroom (`3m ✓`, `2m / ~4m`, `⚠ overdue`).

Out of scope (needs backend `CheckRow` fields): runner-wait patterns, flake annotations, blocked-on text — follow-up issue to file.

Spec: docs/superpowers/specs/2026-07-04-plotroom-progress-bars-design.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge --squash --auto
```

- [ ] **Step 4: File the follow-up issue** for the out-of-scope plotroom sub-states (runner-wait / flake / blocked-on) so the omission is tracked, per the spec's Non-goals.

---

## Self-Review

- **Spec coverage:** shared scale (T1) ✓; skipped/queued + textures (T2) ✓; GanttBar primitive removing duplication (T3) ✓; CheckRowList rewrite + labels + 15% stub (T4) ✓; StageTrack routing (T5) ✓; dead-code purge + gates + PR + follow-up (T6) ✓. Non-goals explicitly deferred ✓.
- **Placeholder scan:** none — every code/test step is concrete.
- **Type consistency:** `GanttRow`/`GanttSegments`/`expectedTotals`/`ganttScale`/`ganttSegments` defined in T1 and consumed with identical signatures in T4; `checkStateColor`/`checkStateTexture`/`CheckVisualState` from T2 used in T4/T5; `GanttBarProps` from T3 used in T4/T5. `data-band`/`data-testid`/`data-state` contract preserved for existing tests.
