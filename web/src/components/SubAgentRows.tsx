import { useNavigate } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { subAgentTreeFromSummaries, type SubAgentNode } from '@/session/selectors'
import type { SubAgentSummary } from '@/api/types'

const DOT: Record<string, string> = {
  running: 'bg-blue-500 animate-pulse',
  done: 'bg-emerald-500',
  failed: 'bg-red-500',
}

function Row({ sessionId, node, depth }: { sessionId: string; node: SubAgentNode; depth: number }) {
  const navigate = useNavigate()
  return (
    <>
      <button
        type="button"
        onClick={() => navigate(`/session/${sessionId}/subagent/${node.agentId}`)}
        style={{ paddingLeft: 12 + Math.min(depth, 6) * 16 }}
        className={cn(
          'flex w-full items-center gap-2 py-1.5 pr-2 text-left text-xs min-h-[36px] rounded',
          'text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700/40',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
        )}
        aria-label={`Review sub-agent ${node.agentType}: ${node.description}`}
      >
        <span aria-hidden className={cn('h-2 w-2 flex-shrink-0 rounded-full', DOT[node.status] ?? 'bg-gray-400')} />
        <span className="font-medium text-gray-800 dark:text-gray-200">{node.agentType}</span>
        {node.description && <span className="truncate opacity-80">{node.description}</span>}
      </button>
      {node.children.map((c) => (
        <Row key={c.agentId} sessionId={sessionId} node={c} depth={depth + 1} />
      ))}
    </>
  )
}

/** Nested, recursive sub-agent rows for a session card. Renders nothing when empty. */
export function SubAgentRows({ sessionId, subAgents }: { sessionId: string; subAgents: SubAgentSummary[] }) {
  const tree = subAgentTreeFromSummaries(subAgents)
  if (tree.length === 0) return null
  return (
    <div role="group" aria-label="Sub-agents" className="mt-1 border-t border-gray-100 dark:border-gray-700/60">
      {tree.map((n) => (
        <Row key={n.agentId} sessionId={sessionId} node={n} depth={0} />
      ))}
    </div>
  )
}
