import { subAgentTreeFromSummaries, type SubAgentNode } from '@/session/selectors'
import type { SubAgentSummary } from '@/api/types'
import { cn } from '@/lib/utils'

const DOT: Record<string, string> = {
  running: 'bg-amber-500 animate-pulse',
  done: 'bg-green-500',
  failed: 'bg-red-500',
}

function Row({
  sessionId,
  node,
  depth,
  onOpenSubAgent,
}: {
  sessionId: string
  node: SubAgentNode
  depth: number
  onOpenSubAgent: (sessionId: string, agentId: string) => void
}) {
  return (
    <>
      <button
        type="button"
        onClick={() => onOpenSubAgent(sessionId, node.agentId)}
        style={{ paddingLeft: 12 + Math.min(depth, 6) * 16 }}
        className={cn(
          'flex w-full items-center gap-2 py-1.5 text-left text-xs min-h-[36px]',
          'text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700/40 rounded',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
        )}
        aria-label={`Review sub-agent ${node.type}: ${node.description}`}
      >
        <span
          aria-hidden
          className={cn('w-2 h-2 rounded-full flex-shrink-0', DOT[node.status] ?? 'bg-gray-400')}
        />
        <span className="font-medium">{node.type}</span>
        <span className="truncate opacity-80">{node.description}</span>
      </button>
      {node.children.map((c) => (
        <Row
          key={c.agentId}
          sessionId={sessionId}
          node={c}
          depth={depth + 1}
          onOpenSubAgent={onOpenSubAgent}
        />
      ))}
    </>
  )
}

export function SubAgentRows({
  sessionId,
  subAgents,
  depth,
  onOpenSubAgent,
}: {
  sessionId: string
  subAgents: SubAgentSummary[]
  depth: number
  onOpenSubAgent: (sessionId: string, agentId: string) => void
}) {
  const tree = subAgentTreeFromSummaries(subAgents)
  if (tree.length === 0) return null
  return (
    <div role="group" aria-label="Sub-agents" className="mt-1 border-t border-gray-100 dark:border-gray-700/60">
      {tree.map((n) => (
        <Row
          key={n.agentId}
          sessionId={sessionId}
          node={n}
          depth={depth}
          onOpenSubAgent={onOpenSubAgent}
        />
      ))}
    </div>
  )
}
