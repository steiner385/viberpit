import { useRef, useEffect, useCallback, useMemo } from 'react'
import { useVirtualizer, elementScroll, type VirtualizerOptions } from '@tanstack/react-virtual'
import type { Turn as TurnType, RotationMarker, SubThread } from '@/session/reducer'
import type { ToolDetailMode } from '@/session/groupBlocks'
import { Turn } from './Turn'
import { RotationDivider } from './RotationDivider'
import { ModelSwitchDivider } from './ModelSwitchDivider'
import { modelName } from '@/session/modelName'
import { buildRenderRows, type RenderRow } from './transcriptRows'
import { prefetchMarkdown } from './markdownChunk'
import { useStickToBottom } from './useStickToBottom'
import { ScrollToBottomButton } from './ScrollToBottomButton'

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
  /** Nested sub-agent threads keyed by spawning Task toolId. Absent in read-only views (e.g. the archived reader). */
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
  /** Changes on every local send (pass the runtime's `pendingSentSeq`): jumps to the
   *  bottom and re-enables auto-follow even if the user had scrolled up. */
  followSignal?: unknown
}

/**
 * TranscriptView — the session transcript. Renders every turn, interleaved
 * rotation dividers, and model-switch dividers through a single windowed list
 * (@tanstack/react-virtual): only the rows near the viewport are in the DOM, so
 * a 2000-turn session stays a handful of nodes instead of growing unbounded.
 *
 * Scroll behavior is unchanged from the render-all version it replaces:
 *  - stick-to-bottom (`useStickToBottom`) follows content all the way down while
 *    the user is at the bottom (a ResizeObserver on the spacer catches the
 *    virtualizer's async remeasurement — late markdown/font/image layout,
 *    expanding a tool group), and a local send re-sticks (`followSignal`),
 *  - scrolling up to read history latches auto-pin off — only a real user
 *    gesture counts; our own pins and the virtualizer's row-remeasure scroll
 *    adjustments both go through a programmatic-scroll guard,
 *  - scrolling near the top pages in older turns and the prior scroll anchor is
 *    restored so the viewport doesn't jump.
 */
export function TranscriptView({ turns, rotations = [], model, toolDetailMode, thinkingCollapsed, streamingBlockId = null, onAnswer, subAgents = {}, earliestTurn, loadingOlder, onLoadOlder, variant = 'session', onAsk, followSignal }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  // Warm the markdown renderer the moment a transcript exists, rather than
  // letting the first `<Markdown>` request it while the user waits to read that
  // very message (#1411). Every transcript surface — live session, sub-agent
  // reader, archived review — mounts this view, so this is the one place it is
  // needed. Idempotent: the module registry dedupes repeat imports.
  useEffect(() => { void prefetchMarkdown() }, [])
  const {
    pinnedRef, showScrollButton, handleScroll: handleStickScroll, scrollToBottom,
    scrollProgrammatically, markProgrammaticScroll,
  } = useStickToBottom({ scrollRef: containerRef, contentRef, followSignal })
  // Snapshot of scrollHeight taken just before onLoadOlder() is called, used to
  // restore the scroll anchor after older turns are prepended.
  const prevScrollHeightRef = useRef<number | null>(null)

  const lastTurnId = turns.length ? turns[turns.length - 1].id : null

  // The full ordered render list: head markers, then the flattened transcript.
  // buildRenderRows is the single source of truth for which rows exist.
  const rows = useMemo<RenderRow[]>(
    () => buildRenderRows(turns, rotations, { loadingOlder, earliestTurn }),
    [turns, rotations, loadingOlder, earliestTurn],
  )

  // The virtualizer moves scrollTop on its own: when a row *above* the viewport
  // measures for the first time (or is remeasured), virtual-core compensates
  // the estimate→actual delta so the reader's viewport stays anchored on the
  // same row. On entry that is every overscan row above the viewport at once,
  // and since a "Thinking" row is ~40px against a 120px estimate the sum is
  // easily a few hundred px. Left on the library default that scroll is
  // indistinguishable from a user gesture: handleScroll reads "scrolled up",
  // auto-pin latches off, and the session opens a little above the true bottom
  // with the Scroll-to-bottom button showing. Owning scrollToFn is the one seam
  // that routes every library-initiated scroll through the same guard as ours.
  // While pinned, the bottom — not the anchored row — is the invariant, so after
  // the library's move we snap straight back to it; the spacer ResizeObserver
  // below then follows the committed height as usual.
  const scrollToFn = useCallback<VirtualizerOptions<HTMLDivElement, Element>['scrollToFn']>((offset, opts, instance) => {
    markProgrammaticScroll()
    elementScroll(offset, opts, instance)
    const el = containerRef.current
    if (pinnedRef.current && el) el.scrollTop = el.scrollHeight
  }, [markProgrammaticScroll, pinnedRef])

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
    scrollToFn,
  })

  const totalSize = virtualizer.getTotalSize()

  // Update stickiness on scroll; trigger paging when near the top.
  function handleScroll() {
    const el = containerRef.current
    // Skip scrolls we caused ourselves. During entry the whole transcript
    // replays and then keeps growing as markdown/fonts/images lay out late; each
    // pin sets scrollTop to the *then* bottom, but its scroll event arrives after
    // more growth, so without this guard it reads as "user scrolled up" and the
    // auto-pin latches off above the true bottom.
    if (!el || !handleStickScroll()) return
    if (el.scrollTop <= TOP_THRESHOLD && (earliestTurn ?? 0) > 0 && !loadingOlder && onLoadOlder) {
      prevScrollHeightRef.current = el.scrollHeight // anchor before the prepend
      onLoadOlder()
    }
  }

  // Preserve scroll position when older turns are prepended (turns grew at the top).
  // Runs before useStickToBottom's ResizeObserver pin can fire; the observer
  // early-returns while the user is reading history (unpinned), so the two don't fight.
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    if (prevScrollHeightRef.current != null) {
      scrollProgrammatically(el, el.scrollTop + el.scrollHeight - prevScrollHeightRef.current)
      prevScrollHeightRef.current = null
    }
  }, [turns, scrollProgrammatically])

  const virtualRows = virtualizer.getVirtualItems()

  function renderRow(row: RenderRow) {
    switch (row.kind) {
      case 'loading-older':
        return <div className="py-2 text-center text-xs text-ink-muted">Loading earlier history…</div>
      case 'begin':
        return <div className="py-2 text-center text-xs text-ink-muted">— Beginning of session —</div>
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
            positioned at their measured offsets. useStickToBottom's ResizeObserver
            watches this element, so its height (=== getTotalSize()) driving the pin is the
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

      {showScrollButton && <ScrollToBottomButton onClick={() => scrollToBottom({ smooth: true })} />}
    </div>
  )
}
