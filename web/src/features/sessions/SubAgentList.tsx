import { cn } from '@/lib/utils'
import { useNow } from '@/hooks/useNow'
import { agentDuration } from './subAgentDuration'
import { STATUS_TONE, STATUS_LABEL } from './subAgentStatus'
import type { SubAgent } from '@/session/selectors'

interface Props {
  subAgents: SubAgent[]
}

/** Presentational list of sub-agents (derived from Task tool calls). */
export function SubAgentList({ subAgents }: Props) {
  // Only tick while something is actually running (and the tab is visible).
  const now = useNow(1000, subAgents.some((a) => a.status === 'running'))
  return (
    <ul className="flex flex-col gap-1 py-1" aria-label="Sub-agents">
      {subAgents.map((a) => {
        const duration = agentDuration(a, now)
        return (
          <li key={a.toolId} className="flex items-start gap-2 px-1 text-xs">
            <span
              className={cn('mt-1 h-2 w-2 flex-shrink-0 rounded-full', STATUS_TONE[a.status])}
              aria-hidden="true"
            />
            <span className="flex min-w-0 flex-1 flex-col">
              <span className="flex items-center gap-1.5">
                <span className="truncate font-medium text-ink">{a.type}</span>
                <span className="text-2xs uppercase tracking-wide text-ink-muted">
                  {STATUS_LABEL[a.status]}
                </span>
                {duration && (
                  <span
                    className="ml-auto flex-shrink-0 tabular-nums text-ink-muted"
                    title={`${a.status === 'running' ? 'Running for' : 'Ran for'} ${duration}`}
                  >
                    {duration}
                  </span>
                )}
              </span>
              {(a.activity || a.description) && (
                <span className="text-ink-muted line-clamp-2">
                  {a.status === 'running' && a.activity ? a.activity : a.description}
                </span>
              )}
            </span>
          </li>
        )
      })}
    </ul>
  )
}
