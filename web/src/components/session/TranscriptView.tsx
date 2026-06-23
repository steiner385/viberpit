import { useRef, useEffect, Fragment } from 'react'
import type { Turn as TurnType, RotationMarker, SubThread } from '@/session/reducer'
import type { ToolDetailMode } from '@/session/groupBlocks'
import { Turn } from './Turn'
import { RotationDivider } from './RotationDivider'

interface Props {
  turns: TurnType[]
  rotations?: RotationMarker[]
  permissionMode: string
  toolDetailMode: ToolDetailMode
  thinkingCollapsed: boolean
  /** Id of the live-streaming block (null when idle/replaying). */
  streamingBlockId?: string | null
  onDecision: (permId: string, decision: 'allow' | 'deny' | 'always') => void
  onAnswer: (questionId: string, selected: string[], mode?: 'option' | 'freeform' | 'chat') => void
  /** Nested sub-agent threads keyed by spawning Task toolId. Absent in read-only views (e.g. ReviewView). */
  subAgents?: Record<string, SubThread>
  /** The earliest turn index known to the server (0 = at the very start). Absent in read-only views. */
  earliestTurn?: number
  /** True while an older-page fetch is in flight. */
  loadingOlder?: boolean
  /** Called when the user scrolls near the top and more history is available. */
  onLoadOlder?: () => void
}

/**
 * Group rotations by their anchor turn id so each can be rendered immediately
 * after the turn it followed. Rotations with a null anchor (they arrived before
 * any turn) are returned separately and render at the very top.
 */
function groupRotationsByAnchor(rotations: RotationMarker[]) {
  const byTurn = new Map<string, RotationMarker[]>()
  const top: RotationMarker[] = []
  for (const r of rotations) {
    if (r.afterTurnId === null) {
      top.push(r)
      continue
    }
    const list = byTurn.get(r.afterTurnId) ?? []
    list.push(r)
    byTurn.set(r.afterTurnId, list)
  }
  return { byTurn, top }
}

export function TranscriptView({ turns, rotations = [], permissionMode, toolDetailMode, thinkingCollapsed, streamingBlockId = null, onDecision, onAnswer, subAgents = {}, earliestTurn, loadingOlder, onLoadOlder }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  // Track whether the user is at/near the bottom so we auto-scroll only when appropriate.
  const isNearBottomRef = useRef(true)
  // Snapshot of scrollHeight taken just before onLoadOlder() is called, used to
  // restore the scroll anchor after older turns are prepended.
  const prevScrollHeightRef = useRef<number | null>(null)

  const TOP_THRESHOLD = 64

  // Update isNearBottom on scroll; trigger paging when near the top.
  function handleScroll() {
    const el = containerRef.current
    if (!el) return
    const threshold = 80 // px from bottom counts as "near bottom"
    isNearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight <= threshold
    if (el.scrollTop <= TOP_THRESHOLD && (earliestTurn ?? 0) > 0 && !loadingOlder && onLoadOlder) {
      prevScrollHeightRef.current = el.scrollHeight // anchor before the prepend
      onLoadOlder()
    }
  }

  // Preserve scroll position when older turns are prepended (turns grew at the top).
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    if (prevScrollHeightRef.current != null) {
      el.scrollTop += el.scrollHeight - prevScrollHeightRef.current
      prevScrollHeightRef.current = null
    }
  }, [turns])

  // Auto-scroll the container (not the sentinel node) when turns OR rotations change
  useEffect(() => {
    if (!isNearBottomRef.current) return
    const el = containerRef.current
    if (el) {
      el.scrollTop = el.scrollHeight
    }
  }, [turns, rotations])

  const { byTurn, top } = groupRotationsByAnchor(rotations)
  // Defensive: rotations anchored to a turn that isn't present (shouldn't happen
  // in normal flow) are surfaced after the transcript rather than silently lost.
  const knownIds = new Set(turns.map((t) => t.id))
  const orphans = rotations.filter((r) => r.afterTurnId !== null && !knownIds.has(r.afterTurnId))

  return (
    <div
      ref={containerRef}
      onScroll={handleScroll}
      className="flex-1 overflow-y-auto"
    >
      {/* Older-page loading spinner */}
      {loadingOlder && (
        <div className="py-2 text-center text-xs text-gray-400">Loading earlier history…</div>
      )}
      {/* Beginning-of-session marker — shown once earliestTurn reaches 0 and there are turns */}
      {turns.length > 0 && (earliestTurn ?? 0) === 0 && (
        <div className="py-2 text-center text-xs text-gray-400">— Beginning of session —</div>
      )}

      {/* Rotations that occurred before any turn arrived */}
      {top.map((r, i) => (
        <RotationDivider key={`rot-top-${i}`} from={r.from} to={r.to} reason={r.reason} />
      ))}

      {turns.map((turn, idx) => (
        <Fragment key={turn.id}>
          <Turn
            turn={turn}
            permissionMode={permissionMode}
            isLastTurn={idx === turns.length - 1}
            toolDetailMode={toolDetailMode}
            thinkingCollapsed={thinkingCollapsed}
            // The open block only ever lives in the last turn; pass null to the
            // rest so their memoized render stays stable across deltas.
            streamingBlockId={idx === turns.length - 1 ? streamingBlockId : null}
            onDecision={onDecision}
            onAnswer={onAnswer}
            subAgents={subAgents}
          />
          {(byTurn.get(turn.id) ?? []).map((r, i) => (
            <RotationDivider key={`rot-${turn.id}-${i}`} from={r.from} to={r.to} reason={r.reason} />
          ))}
        </Fragment>
      ))}

      {orphans.map((r, i) => (
        <RotationDivider key={`rot-orphan-${i}`} from={r.from} to={r.to} reason={r.reason} />
      ))}
    </div>
  )
}
