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
                    style={{ width: `${overrun ? 100 : seg.solidPct}%` }} aria-hidden />
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
