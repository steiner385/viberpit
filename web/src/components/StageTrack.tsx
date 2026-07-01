import { Check, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { StageModel, NodeState } from '@/api/types'
import { formatEta } from '@/lib/formatEta'
import { nodeCircleClass } from '@/lib/checkState'
import { bandSegments, pseudoElapsedFromPercent, isOverrun } from '@/lib/etaBand'
import { useNow } from '@/hooks/useNow'

// Fallback skeleton shown when no model is available yet.
const SKELETON_NODES = [
  { id: 'ci', label: 'CI' },
  { id: 'ready', label: 'Ready' },
  { id: 'merged', label: 'Merged' },
]

// Connector line tint: green when upstream is done, gray otherwise.
function connectorClass(upstreamState: NodeState | 'pending'): string {
  return upstreamState === 'done'
    ? 'bg-green-400 dark:bg-green-600'
    : 'bg-gray-200 dark:bg-gray-700'
}

interface StageTrackProps {
  model: StageModel | undefined
  states: { id: string; state: NodeState }[] | undefined
  updatedAt: number
  etaSeconds?: number | null
  etaOverdue?: boolean
  etaLowSeconds?: number | null
  etaHighSeconds?: number | null
  etaPercent?: number | null
}

export function StageTrack({ model, states, updatedAt, etaSeconds, etaOverdue, etaLowSeconds, etaHighSeconds, etaPercent }: StageTrackProps) {
  const now = useNow()
  // Build a lookup of state by node id.
  const stateById: Record<string, NodeState> = {}
  if (states) {
    for (const s of states) stateById[s.id] = s.state
  }

  // Determine the nodes to render.
  const nodes: { id: string; label: string; kind?: string }[] = model ? model.nodes : SKELETON_NODES

  return (
    <div className="flex items-center gap-0" role="img" aria-label="Stage track">
      {nodes.map((node, idx) => {
        const state: NodeState = stateById[node.id] ?? 'pending'
        const isFirst = idx === 0
        const prev = idx > 0 ? (stateById[nodes[idx - 1].id] ?? 'pending') : 'pending'
        const isCiNode = node.kind === 'ci'
        const showEta = isCiNode && state === 'active' && (etaSeconds != null || etaOverdue)

        return (
          <div key={node.id} className="flex items-center">
            {/* Connector line before this node (skip for first) */}
            {!isFirst && (() => {
              const prevNode = nodes[idx - 1]
              const liveDeltaS = Math.max(0, (now - updatedAt) / 1000)
              const showBand = prevNode?.kind === 'ci' && prev === 'active'
                && etaPercent != null && etaLowSeconds != null && etaHighSeconds != null
              const seg = showBand
                ? bandSegments({
                    elapsedS: pseudoElapsedFromPercent(etaSeconds ?? 0, etaPercent!) + liveDeltaS,
                    lowS: etaLowSeconds!, nominalS: etaSeconds ?? 0, highS: etaHighSeconds!,
                  })
                : null
              const overrun = isOverrun(liveDeltaS, etaHighSeconds ?? null)
              const bandColor = overrun ? 'bg-amber-500' : 'bg-accent'
              if (seg) {
                return (
                  <div data-testid="ci-band" className="relative h-1.5 w-10 shrink-0 overflow-hidden rounded bg-gray-200 dark:bg-gray-700">
                    <span className={cn('absolute inset-y-0 left-0 rounded-l', bandColor)} style={{ width: `${overrun ? 100 : seg.solidPct}%` }} aria-hidden />
                    <span className={cn('absolute inset-y-0 opacity-30', bandColor)} style={{ left: `${seg.bandLeftPct}%`, width: `${seg.bandWidthPct}%` }} aria-hidden />
                    <span className="absolute inset-y-[-1px] w-px bg-foreground" style={{ left: `${seg.tickPct}%` }} aria-hidden />
                  </div>
                )
              }
              return <div className={cn('h-0.5 w-6 shrink-0', connectorClass(prev))} aria-hidden />
            })()}
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
