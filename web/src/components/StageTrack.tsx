import { Check, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { StageModel, NodeState } from '@/api/types'
import { formatEta } from './PipelineView'

// Fallback skeleton shown when no model is available yet.
const SKELETON_NODES = [
  { id: 'ci', label: 'CI' },
  { id: 'ready', label: 'Ready' },
  { id: 'merged', label: 'Merged' },
]

// Map a node state to the Tailwind classes for the circle.
function nodeCircleClass(state: NodeState | 'pending'): string {
  switch (state) {
    case 'done':
      return 'bg-green-500 border-green-500 text-white dark:bg-green-500 dark:border-green-500'
    case 'active':
      return 'bg-accent border-accent text-accent-foreground ring-2 ring-accent/40'
    case 'failed':
      return 'bg-red-500 border-red-500 text-white dark:bg-red-500 dark:border-red-500'
    case 'pending':
    default:
      return 'bg-transparent border-gray-300 dark:border-gray-600 text-gray-400 dark:text-gray-500'
  }
}

// Connector line tint: green when upstream is done, gray otherwise.
function connectorClass(upstreamState: NodeState | 'pending'): string {
  return upstreamState === 'done'
    ? 'bg-green-400 dark:bg-green-600'
    : 'bg-gray-200 dark:bg-gray-700'
}

interface StageTrackProps {
  model: StageModel | undefined
  states: { id: string; state: NodeState }[] | undefined
  etaSeconds?: number | null
  etaOverdue?: boolean
}

export function StageTrack({ model, states, etaSeconds, etaOverdue }: StageTrackProps) {
  // Build a lookup of state by node id.
  const stateById: Record<string, NodeState> = {}
  if (states) {
    for (const s of states) stateById[s.id] = s.state
  }

  // Determine the nodes to render.
  const nodes: { id: string; label: string }[] = model ? model.nodes : SKELETON_NODES

  return (
    <div className="flex items-center gap-0" aria-label="Stage track">
      {nodes.map((node, idx) => {
        const state: NodeState = stateById[node.id] ?? 'pending'
        const isFirst = idx === 0
        const prev = idx > 0 ? (stateById[nodes[idx - 1].id] ?? 'pending') : 'pending'
        const isCiNode = node.id === 'ci'
        const showEta = isCiNode && state === 'active' && (etaSeconds != null || etaOverdue)

        return (
          <div key={node.id} className="flex items-center">
            {/* Connector line before this node (skip for first) */}
            {!isFirst && (
              <div className={cn('h-0.5 w-6 shrink-0', connectorClass(prev))} aria-hidden />
            )}
            {/* Node column: circle + label */}
            <div className="flex flex-col items-center gap-0.5">
              <div
                data-testid={`stage-node-${node.id}`}
                data-state={state}
                className={cn(
                  'flex h-5 w-5 items-center justify-center rounded-full border-2',
                  nodeCircleClass(state),
                )}
              >
                {state === 'done' && <Check size={10} aria-hidden strokeWidth={3} />}
                {state === 'failed' && <X size={10} aria-hidden strokeWidth={3} />}
              </div>
              <span className="text-[10px] leading-none text-muted-foreground whitespace-nowrap">
                {node.label}
              </span>
              {showEta && (
                <span className="text-[10px] leading-none text-muted-foreground whitespace-nowrap">
                  {etaOverdue ? 'overdue' : `~${formatEta(etaSeconds!)}`}
                </span>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}
