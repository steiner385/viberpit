import { useRef, useEffect, Fragment } from 'react'
import type { Turn as TurnType, RotationMarker, SubThread } from '@/session/reducer'
import type { ToolDetailMode } from '@/session/groupBlocks'
import { Turn } from './Turn'
import { RotationDivider } from './RotationDivider'

interface Props {
  turns: TurnType[]
  rotations?: RotationMarker[]
  toolDetailMode: ToolDetailMode
  thinkingCollapsed: boolean
  /** Id of the live-streaming block (null when idle/replaying). */
  streamingBlockId?: string | null
  onAnswer: (questionId: string, selected: string[], mode?: 'option' | 'freeform' | 'chat') => void
  /** Nested sub-agent threads keyed by spawning Task toolId. Absent in read-only views (e.g. ReviewView). */
  subAgents?: Record<string, SubThread>
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

export function TranscriptView({ turns, rotations = [], toolDetailMode, thinkingCollapsed, streamingBlockId = null, onAnswer, subAgents = {} }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  // Track whether the user is at/near the bottom so we auto-scroll only when appropriate.
  const isNearBottomRef = useRef(true)

  // Update isNearBottom on scroll
  function handleScroll() {
    const el = containerRef.current
    if (!el) return
    const threshold = 80 // px from bottom counts as "near bottom"
    isNearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight <= threshold
  }

  // Pin to the true bottom whenever the content height changes — not just when
  // turns/rotations change. Entering an active session replays the whole
  // transcript at once, then keeps growing as markdown, syntax highlighting,
  // web fonts and images lay out asynchronously *after* React commits. A prop
  // effect can't see that late growth, so a synchronous scroll would land above
  // the real bottom. A ResizeObserver on the content fires post-layout on every
  // height change (including the initial observe), so we follow content all the
  // way down — but only while the user hasn't scrolled up to read history.
  useEffect(() => {
    const content = contentRef.current
    if (!content) return
    const ro = new ResizeObserver(() => {
      if (!isNearBottomRef.current) return
      const el = containerRef.current
      if (el) el.scrollTop = el.scrollHeight
    })
    ro.observe(content)
    return () => ro.disconnect()
  }, [])

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
      {/* Inner wrapper so the ResizeObserver above measures content height; the
          scroll container itself has a fixed flex height and never reports growth. */}
      <div ref={contentRef}>
        {/* Rotations that occurred before any turn arrived */}
        {top.map((r, i) => (
          <RotationDivider key={`rot-top-${i}`} from={r.from} to={r.to} reason={r.reason} />
        ))}

        {turns.map((turn, idx) => (
          <Fragment key={turn.id}>
            <Turn
              turn={turn}
              isLastTurn={idx === turns.length - 1}
              toolDetailMode={toolDetailMode}
              thinkingCollapsed={thinkingCollapsed}
              // The open block only ever lives in the last turn; pass null to the
              // rest so their memoized render stays stable across deltas.
              streamingBlockId={idx === turns.length - 1 ? streamingBlockId : null}
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
    </div>
  )
}
