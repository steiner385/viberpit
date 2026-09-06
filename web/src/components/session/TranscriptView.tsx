import { useRef, useEffect, useCallback, useMemo, useState } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import type { Turn as TurnType, RotationMarker, SubThread } from '@/session/reducer'
import type { ToolDetailMode } from '@/session/groupBlocks'
import { Turn } from './Turn'
import { RotationDivider } from './RotationDivider'
import { ModelSwitchDivider } from './ModelSwitchDivider'
import { modelName } from '@/session/modelName'
import { buildRenderRows, type RenderRow } from './transcriptRows'

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
  /** 'assistant' marks the AI drawer's transcript (a forward-compat hook for a later
   *  toolbar — e.g. hiding open-in-sessions/archive affordances that don't apply to
   *  the assistant's own thread; none exist inline in this component today). Rendered
   *  as `data-variant` only; default 'session' keeps every existing caller unchanged. */
  variant?: 'session' | 'assistant'
  /** Wired to a noticeCard's "Ask about it" button (assistant variant only — see Turn). */
  onAsk?: (text: string) => void
}

/**
 * TranscriptView — the session transcript. Renders every turn, interleaved
 * rotation dividers, and model-switch dividers through a single windowed list
 * (@tanstack/react-virtual): only the rows near the viewport are in the DOM, so
 * a 2000-turn session stays a handful of nodes instead of growing unbounded.
 *
 * Scroll behavior is unchanged from the render-all version it replaces:
 *  - stick-to-bottom follows content all the way down while the user is at the
 *    bottom (a ResizeObserver on the spacer catches the virtualizer's async
 *    remeasurement — late markdown/font/image layout, expanding a tool group),
 *  - scrolling up to read history latches auto-pin off,
 *  - scrolling near the top pages in older turns and the prior scroll anchor is
 *    restored so the viewport doesn't jump.
 */
export function TranscriptView({ turns, rotations = [], model, toolDetailMode, thinkingCollapsed, streamingBlockId = null, onAnswer, subAgents = {}, earliestTurn, loadingOlder, onLoadOlder, variant = 'session', onAsk }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  // Track whether the user is at/near the bottom so we auto-scroll only when appropriate.
  const isNearBottomRef = useRef(true)
  // Drives the scroll-to-bottom button — separate from the ref so React re-renders when it flips.
  const [showScrollButton, setShowScrollButton] = useState(false)
  // Snapshot of scrollHeight taken just before onLoadOlder() is called, used to
  // restore the scroll anchor after older turns are prepended.
  const prevScrollHeightRef = useRef<number | null>(null)
  // True while a scroll we initiated (pin-to-bottom or prepend anchor) is in
  // flight. The browser delivers the resulting scroll event asynchronously, so
  // we must ignore it: only a real user gesture should change isNearBottom.
  const programmaticScrollRef = useRef(false)

  const lastTurnId = turns.length ? turns[turns.length - 1].id : null

  // The full ordered render list: head markers, then the flattened transcript.
  // buildRenderRows is the single source of truth for which rows exist.
  const rows = useMemo<RenderRow[]>(
    () => buildRenderRows(turns, rotations, { loadingOlder, earliestTurn }),
    [turns, rotations, loadingOlder, earliestTurn],
  )

  // @tanstack/react-virtual returns uncacheable functions by design; the React
  // Compiler skips memoizing this hook's result, which is correct here (we read
  // getTotalSize()/getVirtualItems() fresh every render).
  // eslint-disable-next-line react-hooks/incompatible-library
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => containerRef.current,
    estimateSize: () => 120,
    overscan: 8,
    // Key by stable row identity so cached row measurements survive prepends
    // (paging in older turns) — only the newly added rows fall back to estimate.
    getItemKey: (i) => rows[i].key,
  })

  const totalSize = virtualizer.getTotalSize()

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
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight <= threshold
    if (nearBottom !== isNearBottomRef.current) setShowScrollButton(!nearBottom)
    isNearBottomRef.current = nearBottom
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
  // web fonts and images lay out asynchronously *after* React commits. With
  // virtualization that late growth surfaces as the virtualizer remeasuring the
  // last row and enlarging the spacer (its height === getTotalSize()), so a
  // ResizeObserver on the spacer fires post-layout on every height change
  // (including the initial observe). We follow content all the way down — but
  // only while the user hasn't scrolled up to read history.
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

  const virtualRows = virtualizer.getVirtualItems()

  function renderRow(row: RenderRow) {
    switch (row.kind) {
      case 'loading-older':
        return <div className="py-2 text-center text-xs text-gray-400">Loading earlier history…</div>
      case 'begin':
        return <div className="py-2 text-center text-xs text-gray-400">— Beginning of session —</div>
      case 'model-switch':
        return <ModelSwitchDivider to={modelName(row.toModel)} />
      case 'rotation':
        return <RotationDivider from={row.rot.from} to={row.rot.to} reason={row.rot.reason} />
      case 'turn':
        return (
          <Turn
            turn={row.turn}
            isLastTurn={row.turn.id === lastTurnId}
            toolDetailMode={toolDetailMode}
            thinkingCollapsed={thinkingCollapsed}
            // The open block only ever lives in the last turn; pass null to the
            // rest so their memoized render stays stable across deltas.
            streamingBlockId={row.turn.id === lastTurnId ? streamingBlockId : null}
            onAnswer={onAnswer}
            subAgents={subAgents}
            sessionModel={model}
            variant={variant}
            onAsk={onAsk}
          />
        )
    }
  }

  function scrollToBottom() {
    const el = containerRef.current
    if (!el) return
    isNearBottomRef.current = true
    setShowScrollButton(false)
    scrollProgrammatically(el, el.scrollHeight)
  }

  return (
    <div className="relative flex-1 min-h-0 flex flex-col" data-variant={variant}>
      <div
        ref={containerRef}
        onScroll={handleScroll}
        data-testid="transcript-scroll"
        // overflow-x-hidden is the backstop: an overflow-y-auto box makes
        // overflow-x compute to auto, so any descendant wider than the pane (a
        // stray wide image/embed) would scroll the WHOLE pane sideways. Wrappable
        // content (inline code, links) wraps and code/tables self-scroll via CSS;
        // this clips anything else to the pane instead of scrolling it.
        className="flex-1 overflow-y-auto overflow-x-hidden"
      >
        {/* The spacer reserves the full virtual height so the scrollbar reflects the
            whole transcript; only the windowed rows are mounted, absolutely
            positioned at their measured offsets. The ResizeObserver above watches
            this element, so its height (=== getTotalSize()) driving the pin is the
            same signal the render-all version got from real content growth. */}
        <div ref={contentRef} style={{ height: totalSize, position: 'relative', width: '100%' }}>
          {virtualRows.map((vi) => (
            <div
              key={vi.key}
              data-index={vi.index}
              ref={virtualizer.measureElement}
              style={{ position: 'absolute', top: 0, left: 0, width: '100%', transform: `translateY(${vi.start}px)` }}
            >
              {renderRow(rows[vi.index])}
            </div>
          ))}
        </div>
      </div>

      {showScrollButton && (
        <div className="absolute bottom-4 left-0 right-0 flex justify-center pointer-events-none">
          <button
            onClick={scrollToBottom}
            aria-label="Scroll to bottom"
            className="pointer-events-auto flex items-center gap-1.5 rounded-full border border-gray-200 bg-white px-3 py-1.5 text-xs font-medium text-gray-600 shadow-md transition hover:bg-gray-50 hover:shadow-lg dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700"
          >
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="currentColor" className="size-3.5">
              <path fillRule="evenodd" d="M8 1a.75.75 0 0 1 .75.75v8.69l2.72-2.72a.75.75 0 1 1 1.06 1.06l-4 4a.75.75 0 0 1-1.06 0l-4-4a.75.75 0 0 1 1.06-1.06l2.72 2.72V1.75A.75.75 0 0 1 8 1Z" clipRule="evenodd" />
            </svg>
            Scroll to bottom
          </button>
        </div>
      )}
    </div>
  )
}
