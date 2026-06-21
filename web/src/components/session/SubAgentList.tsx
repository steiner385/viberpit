import { cn } from '@/lib/utils'
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

interface Props {
  subAgents: SubAgent[]
}

/** Presentational list of sub-agents (derived from Task tool calls). */
export function SubAgentList({ subAgents }: Props) {
  return (
    <ul className="flex flex-col gap-1 py-1" aria-label="Sub-agents">
      {subAgents.map((a) => (
        <li key={a.toolId} className="flex items-start gap-2 px-1 text-xs">
          <span
            className={cn('mt-1 h-2 w-2 flex-shrink-0 rounded-full', STATUS_DOT[a.status])}
            aria-hidden="true"
          />
          <span className="flex flex-col">
            <span className="flex items-center gap-1.5">
              <span className="font-medium text-gray-100">{a.type}</span>
              <span className="text-[10px] uppercase tracking-wide text-gray-400">
                {STATUS_LABEL[a.status]}
              </span>
            </span>
            {a.description && (
              <span className="text-gray-400 line-clamp-2">{a.description}</span>
            )}
          </span>
        </li>
      ))}
    </ul>
  )
}
