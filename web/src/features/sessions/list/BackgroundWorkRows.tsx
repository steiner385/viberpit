import { cn } from '@/lib/utils'
import { statusTone } from '@/features/sessions/subAgentStatus'
import { KIND_LABEL } from '@/features/sessions/backgroundWorkSummary'
import type { BackgroundWorkView } from '@/session/selectors'

/** Read-only background-work rows for a session card (#2635): kind + description, and
 *  the armed wakeup. Stop lives in the session view's chip, not on the card. */
export function BackgroundWorkRows({ work }: { work: BackgroundWorkView }) {
  if (work.rows.length === 0 && !work.wakeup) return null
  return (
    <div role="group" aria-label="Background work" className="mt-1 border-t border-edge">
      {work.wakeup && (
        <div className="flex items-center gap-2 py-1.5 pl-3 pr-2 text-xs text-ink-muted min-h-9">
          <span aria-hidden className={cn('h-2 w-2 flex-shrink-0 rounded-full', statusTone('running'))} />
          <span className="font-medium text-ink">wakeup</span><span>armed</span>
        </div>
      )}
      {work.rows.map((r) => (
        <div key={r.toolUseId} className="flex items-center gap-2 py-1.5 pl-3 pr-2 text-xs text-ink-muted min-h-9">
          <span aria-hidden className={cn('h-2 w-2 flex-shrink-0 rounded-full', statusTone('running'))} />
          <span className="font-medium text-ink">{KIND_LABEL[r.kind]}</span>
          {r.description && <span className="truncate opacity-80">{r.description}</span>}
        </div>
      ))}
    </div>
  )
}
