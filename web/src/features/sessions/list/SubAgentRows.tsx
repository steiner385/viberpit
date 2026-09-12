import { useLocation, useNavigate } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { subAgentTreeFromSummaries, type SubAgentNode } from '@/session/selectors'
import { listScopeForPath, subAgentPath } from '@/session/sessionNavState'
import type { SubAgentSummary } from '@/api/types'
import { statusTone } from '@/features/sessions/subAgentStatus'

function Row({ sessionId, node, depth }: { sessionId: string; node: SubAgentNode; depth: number }) {
  const navigate = useNavigate()
  // Stay in the list scope the card is in (Background rows are live sessions
  // under `/sessions/background/…`), so opening a sub-agent does not flip the
  // list column to Active.
  const scope = listScopeForPath(useLocation().pathname)
  return (
    <>
      <button
        type="button"
        onClick={() => navigate(subAgentPath(sessionId, node.agentId, scope))}
        style={{ paddingLeft: 12 + Math.min(depth, 6) * 16 }}
        className={cn(
          'flex w-full items-center gap-2 py-1.5 pr-2 text-left text-xs min-h-9 rounded',
          'text-ink-muted hover:bg-canvas',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
        )}
        aria-label={`Review sub-agent ${node.agentType}: ${node.description}`}
      >
        <span aria-hidden className={cn('h-2 w-2 flex-shrink-0 rounded-full', statusTone(node.status))} />
        <span className="font-medium text-ink">{node.agentType}</span>
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
    <div role="group" aria-label="Sub-agents" className="mt-1 border-t border-edge">
      {tree.map((n) => (
        <Row key={n.agentId} sessionId={sessionId} node={n} depth={0} />
      ))}
    </div>
  )
}
