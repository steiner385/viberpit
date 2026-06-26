import { useRef, useEffect, useCallback, Fragment } from 'react'
import type { Turn as TurnType, RotationMarker, SubThread } from '@/session/reducer'
import type { ToolDetailMode } from '@/session/groupBlocks'
import { Turn } from './Turn'
import { RotationDivider } from './RotationDivider'
import { ModelSwitchDivider } from './ModelSwitchDivider'
import { modelName } from '@/session/modelName'

const TOP_THRESHOLD = 64

interface Props {
  turns: TurnType[]
  rotations?: RotationMarker[]
  /** The session's current model — threaded to badge subagents on a different model. */
  model?: string | null
  toolDetailMode: ToolDetailMode
  thinkingCollapsed: boolean
  /** Id of the live-streaming block (null when idle/replaying). */
  streamingBlockId?: string | null
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

export function TranscriptView({ turns, rotations = [], model, toolDetailMode, thinkingCollapsed, streamingBlockId = null, onAnswer, subAgents = {}, earliestTurn, loadingOlder, onLoadOlder }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  // Track whether the user is at/near the bottom so we auto-scroll only when appropriate.
  const isNearBottomRef = useRef(true)
  // Snapshot of scrollHeight taken just before onLoadOlder() is called, used to
  // restore the scroll anchor after older turns are prepended.
  const prevScrollHeightRef = useRef<number | null>(null)
  // True while a scroll we initiated (pin-to-bottom or prepend anchor) is in
  // flight. The browser delivers the resulting scroll event asynchronously, so
  // we must ignore it: only a real user gesture should change isNearBottom.
  const programmaticScrollRef = useRef(false)

  // Move scrollTop ourselves without the resulting scroll event being mistaken
  // for a user scroll. Cleared on the next frame (not on the scroll event) so it
  // still self-clears when the assignment is a no-op and fires no scroll event.
  const scrollProgrammatically = useCallback((el: HTMLElement, top: number) => {
    programmaticScrollRef.current = true
    el.scrollTop = top
    requestAnimationFrame(() => { programmaticScrollRef.current = false })
  }, [])

  // Update isNearBottom on scroll; trigger paging when near the top.
  function handleScroll() {
    const el = containerRef.current
    if (!el) return
    // Skip scrolls we caused ourselves. During entry the whole transcript
    // replays and then keeps growing as markdown/fonts/images lay out late; each
    // pin sets scrollTop to the *then* bottom, but its scroll event arrives after
    // more growth, so without this guard it reads as "user scrolled up" and the
    // auto-pin latches off above the true bottom.
    if (programmaticScrollRef.current) return
    const threshold = 80 // px from bottom counts as "near bottom"
    isNearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight <= threshold
    if (el.scrollTop <= TOP_THRESHOLD && (earliestTurn ?? 0) > 0 && !loadingOlder && onLoadOlder) {
      prevScrollHeightRef.current = el.scrollHeight // anchor before the prepend
      onLoadOlder()
    }
  }

  // Preserve scroll position when older turns are prepended (turns grew at the top).
  // Runs before the ResizeObserver pin below can fire; the observer early-returns
  // while the user is reading history (isNearBottomRef false), so the two don't fight.
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    if (prevScrollHeightRef.current != null) {
      scrollProgrammatically(el, el.scrollTop + el.scrollHeight - prevScrollHeightRef.current)
      prevScrollHeightRef.current = null
    }
  }, [turns, scrollProgrammatically])

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
      if (el) scrollProgrammatically(el, el.scrollHeight)
    })
    ro.observe(content)
    return () => ro.disconnect()
  }, [scrollProgrammatically])

  const { byTurn, top } = groupRotationsByAnchor(rotations)
  // Defensive: rotations anchored to a turn that isn't present (shouldn't happen
  // in normal flow) are surfaced after the transcript rather than silently lost.
  const knownIds = new Set(turns.map((t) => t.id))
  const orphans = rotations.filter((r) => r.afterTurnId !== null && !knownIds.has(r.afterTurnId))

  return (
    <div
      ref={containerRef}
      onScroll={handleScroll}
      // overflow-x-hidden is the backstop: an overflow-y-auto box makes
      // overflow-x compute to auto, so any descendant wider than the pane (a
      // stray wide image/embed) would scroll the WHOLE pane sideways. Wrappable
      // content (inline code, links) wraps and code/tables self-scroll via CSS;
      // this clips anything else to the pane instead of scrolling it.
      className="flex-1 overflow-y-auto overflow-x-hidden"
    >
      {/* Inner wrapper so the ResizeObserver above measures content height; the
          scroll container itself has a fixed flex height and never reports growth. */}
      <div ref={contentRef}>
        {/* Older-page loading spinner */}
        {loadingOlder && (
          <div className="py-2 text-center text-xs text-gray-400">Loading earlier history…</div>
        )}
        {/* Beginning-of-session marker — only shown when earliestTurn is explicitly 0 (live view at start) */}
        {turns.length > 0 && earliestTurn === 0 && (
          <div className="py-2 text-center text-xs text-gray-400">— Beginning of session —</div>
        )}

        {/* Rotations that occurred before any turn arrived */}
        {top.map((r, i) => (
          <RotationDivider key={`rot-top-${i}`} from={r.from} to={r.to} reason={r.reason} />
        ))}

        {turns.map((turn, idx) => {
          // Show a switch divider when this turn's model differs from the previous
          // turn that had a known model. Only assistant turns carry a model; the
          // first model-bearing turn shows no divider (nothing to differ from).
          let prevModel: string | null = null
          for (let i = idx - 1; i >= 0; i--) {
            if (turns[i].model) { prevModel = turns[i].model ?? null; break }
          }
          const showSwitch = !!turn.model && !!prevModel && turn.model !== prevModel
          return (
          <Fragment key={turn.id}>
            {showSwitch && <ModelSwitchDivider to={modelName(turn.model)} />}
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
              sessionModel={model}
            />
            {(byTurn.get(turn.id) ?? []).map((r, i) => (
              <RotationDivider key={`rot-${turn.id}-${i}`} from={r.from} to={r.to} reason={r.reason} />
            ))}
          </Fragment>
          )
        })}

        {orphans.map((r, i) => (
          <RotationDivider key={`rot-orphan-${i}`} from={r.from} to={r.to} reason={r.reason} />
        ))}
      </div>
    </div>
  )
}
