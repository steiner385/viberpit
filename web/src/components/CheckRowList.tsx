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
  if (c.status === 'QUEUED') return 'queued'
  if (c.conclusion === 'pending' || c.status === 'IN_PROGRESS') return 'active'
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
