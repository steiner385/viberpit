import { useEffect, useState } from 'react'
import { cn } from '@/lib/utils'
import { agentDuration } from './subAgentDuration'
import type { SubAgent, SubAgentStatus } from '@/session/selectors'

const STATUS_DOT: Record<SubAgentStatus, string> = {
  running: 'bg-blue-500 animate-pulse',
  done: 'bg-emerald-500',
  failed: 'bg-red-500',
}

const STATUS_LABEL: Record<SubAgentStatus, string> = {
  running: 'running',
  done: 'done',
  failed: 'failed',
}

/** A `Date.now()` that re-ticks every second while `active`, else stays put. */
function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [active])
  return now
}

interface Props {
  subAgents: SubAgent[]
}

/** Presentational list of sub-agents (derived from Task tool calls). */
export function SubAgentList({ subAgents }: Props) {
  // Only keep a 1s timer alive while something is actually running.
  const now = useNow(subAgents.some((a) => a.status === 'running'))
  return (
    <ul className="flex flex-col gap-1 py-1" aria-label="Sub-agents">
      {subAgents.map((a) => {
        const duration = agentDuration(a, now)
        return (
          <li key={a.toolId} className="flex items-start gap-2 px-1 text-xs">
            <span
              className={cn('mt-1 h-2 w-2 flex-shrink-0 rounded-full', STATUS_DOT[a.status])}
              aria-hidden="true"
            />
            <span className="flex min-w-0 flex-1 flex-col">
              <span className="flex items-center gap-1.5">
                <span className="truncate font-medium text-gray-900 dark:text-gray-100">{a.type}</span>
                <span className="text-[10px] uppercase tracking-wide text-gray-500 dark:text-gray-400">
                  {STATUS_LABEL[a.status]}
                </span>
                {duration && (
                  <span
                    className="ml-auto flex-shrink-0 tabular-nums text-gray-500 dark:text-gray-400"
                    title={`${a.status === 'running' ? 'Running for' : 'Ran for'} ${duration}`}
                  >
                    {duration}
                  </span>
                )}
              </span>
              {a.description && (
                <span className="text-gray-500 line-clamp-2 dark:text-gray-400">{a.description}</span>
              )}
            </span>
          </li>
        )
      })}
    </ul>
  )
}
