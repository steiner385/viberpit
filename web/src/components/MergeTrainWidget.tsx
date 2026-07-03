// web/src/components/MergeTrainWidget.tsx
import type { MergeTrainState, TrainBuild } from '../api/types'

function fmtDuration(secs: number | null): string {
  if (secs == null) return ''
  const m = Math.round(secs / 60)
  if (m < 60) return `~${m}m`
  const h = Math.floor(m / 60)
  return `~${h}h ${m % 60}m`
}

function buildLabel(b: TrainBuild): string {
  const parts: string[] = [`#${b.pr_number}`]
  if (b.percent != null) parts.push(`${b.percent}%`)
  if (b.eta_seconds != null) parts.push(`${fmtDuration(b.eta_seconds)} left`)
  return parts.join(' ')
}

function isIdle(s: MergeTrainState): boolean {
  return s.depth === 0 && s.ejects_24h === 0 && !s.trains_per_hour
    && s.failing.length === 0 && s.blocked.length === 0 && !s.building
}

export function MergeTrainWidget({ state }: { state: MergeTrainState | undefined }) {
  if (!state || isIdle(state)) return null
  const { depth, trains_per_hour, batch_success_pct, ejects_24h, oldest_wait_seconds } = state
  return (
    <div className="mb-2 rounded border border-gray-200 bg-muted/30 p-2 text-xs dark:border-gray-700">
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-muted-foreground">
        <span>depth {depth}</span>
        {trains_per_hour != null && <span>{trains_per_hour.toFixed(1)} trains/hr</span>}
        {batch_success_pct != null && <span>{Math.round(batch_success_pct)}% batch success</span>}
        <span>{ejects_24h} ejects 24h</span>
        {oldest_wait_seconds != null && <span>oldest wait {fmtDuration(oldest_wait_seconds)}</span>}
      </div>
      <div className="mt-1 flex flex-wrap gap-2">
        {state.failing.length > 0 && (
          <span className="rounded border border-red-300 bg-red-50 px-2 py-1 text-red-700 dark:border-red-700 dark:bg-red-900/30 dark:text-red-300">
            ✗ failing {state.failing.map((n) => `#${n}`).join(' ')}
          </span>
        )}
        {state.building && (
          <span className="rounded border border-blue-300 bg-blue-50 px-2 py-1 text-blue-700 dark:border-blue-700 dark:bg-blue-900/30 dark:text-blue-300">
            ▶ group {buildLabel(state.building)}
          </span>
        )}
        {state.blocked.length > 0 && (
          <span className="rounded border border-dashed border-amber-400 bg-amber-50 px-2 py-1 text-amber-700 dark:border-amber-600 dark:bg-amber-900/30 dark:text-amber-300">
            ⊘ blocked behind conflict {state.blocked.map((n) => `#${n}`).join(' ')}
          </span>
        )}
      </div>
      <div className="mt-1 text-muted-foreground">
        Deploy backlog: {state.awaiting_qa} awaiting QA · {state.awaiting_prod} awaiting prod
      </div>
      {state.merges_next && (
        <div className="mt-1 text-foreground">
          ⤳ Merges next: <strong>#{state.merges_next.pr_number}</strong>
          {state.merges_next.percent != null && ` building ${state.merges_next.percent}%`}
          {state.merges_next.eta_seconds != null && ` · ${fmtDuration(state.merges_next.eta_seconds)}`}
        </div>
      )}
    </div>
  )
}
