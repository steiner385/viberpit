import { cn } from '@/lib/utils'
import { useNow } from '@/hooks/useNow'
import { useStopSubAgent } from '@/api/hooks'
import { formatElapsed } from './elapsed'
import { statusTone } from './subAgentStatus'
import { KIND_LABEL } from './backgroundWorkSummary'
import type { BackgroundWorkRow, BackgroundWorkView } from '@/session/selectors'

/** Row status → the shared sub-agent dot vocabulary (`completed` is `done` there). */
const DOT: Record<BackgroundWorkRow['status'], string> = { running: 'running', completed: 'done', failed: 'failed', stopped: 'stopped' }

function elapsedOf(r: BackgroundWorkRow, now: number): string | null {
  if (r.startedAt == null) return null
  // `useNow` is 0 until its first tick — no honest elapsed yet.
  if (r.status === 'running') return now === 0 ? null : formatElapsed(Math.max(0, now - r.startedAt))
  return r.endedAt == null ? null : formatElapsed(Math.max(0, r.endedAt - r.startedAt))
}

/** The "Background" chip's list (#2635): one row per open shell / Monitor / terminal /
 *  workflow with kind, status, elapsed and description, Stop on a live running row
 *  (the same `stop_task` route the sub-agent Stop uses), and the armed wakeup. Rows
 *  are not links — a shell has no transcript to drill into. */
export function BackgroundWorkList({ sessionId, work }: { sessionId: string; work: BackgroundWorkView }) {
  const ticking = work.rows.some((r) => r.status === 'running') || work.wakeupAt != null
  const now = useNow(1000, ticking)
  const stop = useStopSubAgent()
  return (
    <ul className="flex flex-col gap-1 py-1" aria-label="Background work">
      {work.wakeup && (
        <li className="flex items-center gap-2 px-1 py-1 text-xs min-h-target">
          <span className={cn('h-2 w-2 flex-shrink-0 rounded-full', statusTone('running'))} aria-hidden="true" />
          <span className="font-medium text-ink">wakeup</span>
          <span className="text-ink-muted">
            {work.wakeupAt != null && now !== 0 ? `fires in ${formatElapsed(Math.max(0, work.wakeupAt - now))}` : 'armed'}
          </span>
        </li>
      )}
      {work.rows.map((r) => {
        const elapsed = elapsedOf(r, now)
        return (
          <li key={r.toolUseId} className="flex items-start gap-2 px-1 py-1 text-xs min-h-target">
            <span className={cn('mt-1 h-2 w-2 flex-shrink-0 rounded-full', statusTone(DOT[r.status]))} aria-hidden="true" />
            <span className="flex min-w-0 flex-1 flex-col">
              <span className="flex items-center gap-1.5">
                <span className="font-medium text-ink">{KIND_LABEL[r.kind]}</span>
                <span className="text-2xs uppercase tracking-wide text-ink-muted">{r.status}</span>
                {elapsed && <span className="ml-auto flex-shrink-0 tabular-nums text-ink-muted">{elapsed}</span>}
              </span>
              {r.description && <span className="text-ink-muted line-clamp-2 break-all">{r.description}</span>}
            </span>
            {r.live && r.status === 'running' && (
              <button
                type="button"
                onClick={() => stop.mutate({ id: sessionId, toolUseId: r.toolUseId })}
                disabled={stop.isPending}
                aria-label={`Stop ${KIND_LABEL[r.kind]}${r.description ? `: ${r.description}` : ''}`}
                className={cn('flex-shrink-0 rounded px-1.5 py-0.5 text-2xs text-ink-muted min-h-7',
                  'hover:bg-edge focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent')}
              >
                Stop
              </button>
            )}
          </li>
        )
      })}
    </ul>
  )
}
