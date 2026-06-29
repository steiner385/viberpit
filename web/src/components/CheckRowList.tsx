// web/src/components/CheckRowList.tsx
import { cn } from '@/lib/utils'
import { checkStateColor, type CheckVisualState } from '@/lib/checkState'
import { formatEta } from '@/lib/formatEta'
import type { CheckRow } from '@/api/types'

function visualState(c: CheckRow): CheckVisualState {
  if (c.overdue) return 'overdue'
  if (c.conclusion === 'failure') return 'failed'
  if (c.conclusion === 'pending' || c.status === 'IN_PROGRESS' || c.status === 'QUEUED') return 'active'
  if (c.conclusion === 'success') return 'done'
  return 'pending'
}

export function CheckRowList({ checks }: { checks: CheckRow[] }) {
  return (
    <ul className="space-y-0.5">
      {checks.map((c) => {
        const state = visualState(c)
        const done = state === 'done' || state === 'failed'
        return (
          <li
            key={c.name}
            data-testid={`check-row-${c.name}`}
            data-state={state}
            className="flex items-center gap-2 text-xs"
          >
            <span className="w-48 shrink-0 truncate text-muted-foreground">{c.name}</span>
            <span className="relative h-1.5 flex-1 overflow-hidden rounded bg-gray-200 dark:bg-gray-700">
              <span
                className={cn('absolute inset-y-0 left-0 rounded', checkStateColor(state))}
                style={{ width: done ? '100%' : '40%' }}
                aria-hidden
              />
            </span>
            <span className="w-16 shrink-0 text-right tabular-nums text-muted-foreground">
              {c.duration_seconds != null ? formatEta(c.duration_seconds) : '—'}
              {c.overdue ? ' ⚠' : ''}
            </span>
          </li>
        )
      })}
    </ul>
  )
}
