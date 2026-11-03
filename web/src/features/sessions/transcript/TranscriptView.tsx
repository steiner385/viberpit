import { useRef, useEffect, useCallback, useMemo, useState, lazy, Suspense } from 'react'
import { toast } from 'sonner'
import { fetchTranscriptSearch } from '@/api/transcript'
import { useVirtualizer, elementScroll, type Virtualizer, type VirtualizerOptions } from '@tanstack/react-virtual'
import { turnHoldsEngineTurn, type Turn as TurnType, type RotationMarker, type RewindMarker, type SubThread } from '@/session/reducer'
import type { ToolDetailMode } from '@/session/groupBlocks'
import { Turn } from './Turn'
import { RotationDivider } from './RotationDivider'
import { ModelSwitchDivider } from './ModelSwitchDivider'
import { RewindDividerSlot } from '@/features/sessions/rewind/RewindDividerSlot'
import { TurnSummaryCard, type TurnSummaryInputs } from './TurnSummaryCard'
import { modelName } from '@/session/modelName'
import { failedPrompts } from '@/session/rewindTarget'
import { buildRenderRows, ownPromptRowIndex, type RenderRow } from './transcriptRows'
import { prefetchMarkdown } from './markdownChunk'
import { useStickToBottom } from './useStickToBottom'
import { ScrollToBottomButton } from './ScrollToBottomButton'
import { useTranscriptJumpRegistration } from '@/shell/sessionGestures'
import { SESSION_TRANSCRIPT_ATTR } from '@/lib/detachKeyGuard'
import { TranscriptSearchContext, type SearchCurrent, type TranscriptSearchState } from './search/TranscriptSearchContext'
import { loadSearchKit, type SearchKitModule } from './search/loadSearchKit'
import type { JumpRequest, PendingJump } from './search/searchKit'

/** The ⌘F bar (D-11) — in the search kit's chunk: nothing needs it until someone
 *  searches. The kit's renderers and jump logic load with it (`loadSearchKit`). */
const TranscriptSearchBar = lazy(() => loadSearchKit().then((m) => ({ default: m.TranscriptSearchBar })))

const TOP_THRESHOLD = 64
/** How much of a row may peek in at the top before it counts as visible (prompt jumps). */
const SLIVER_PX = 2

/**
 * While a prompt jump holds the view: compensate EVERY resize of a row above
 * the viewport. virtual-core's default skips a re-measured row's delta while the
 * scroll direction is backward — right for a user scrolling up (it avoids the
 * "rows jump under the reader" cascade), wrong for a jump up, where a reply that
 * settles shorter after the jump would drag the prompt off the top.
 */
type HoldRule = NonNullable<Virtualizer<HTMLDivElement, Element>['shouldAdjustScrollPositionOnItemSizeChange']>
const holdJumpedRow: HoldRule = (item, _delta, instance) => item.start < (instance.scrollOffset ?? 0)

interface Props {
  turns: TurnType[]
  rotations?: RotationMarker[]
  /** The latest rewind's divider (D-9), live sessions only. */
  rewind?: RewindMarker | null
  /** The session's current model — threaded to badge subagents on a different model. */
  model?: string | null
  toolDetailMode: ToolDetailMode
  thinkingCollapsed: boolean
  /** The runtime's `viewModeEpoch` (see `Turn`). */
  viewModeEpoch?: number
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
  /** The live session's post-turn summary card (D-11) inputs; it trails the last
   *  turn. Absent in read-only views and the assistant drawer. */
  summary?: TurnSummaryInputs | null
  /** The live session's id: enables the ⌘F transcript search (D-11). Absent in
   *  read-only views and the assistant drawer, which have no search. */
  sessionId?: string
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
export function TranscriptView({ turns, rotations = [], rewind, model, toolDetailMode, thinkingCollapsed, viewModeEpoch, streamingBlockId = null, onAnswer, subAgents = {}, earliestTurn, loadingOlder, onLoadOlder, variant = 'session', onAsk, followSignal, summary, sessionId }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  // Warm the markdown renderer the moment a transcript exists, rather than
  // letting the first `<Markdown>` request it while the user waits to read that
  // very message (#1411). Every transcript surface — live session, sub-agent
  // reader, archived review — mounts this view, so this is the one place it is
  // needed. Idempotent: the module registry dedupes repeat imports.
  useEffect(() => { void prefetchMarkdown() }, [])
  const {
    pinnedRef, showScrollButton, handleScroll: handleStickScroll, scrollToBottom, unpin,
    scrollProgrammatically, markProgrammaticScroll,
  } = useStickToBottom({ scrollRef: containerRef, contentRef, followSignal })
  // Snapshot of scrollHeight taken just before onLoadOlder() is called, used to
  // restore the scroll anchor after older turns are prepended.
  const prevScrollHeightRef = useRef<number | null>(null)

  const lastTurnId = turns.length ? turns[turns.length - 1].id : null

  // The full ordered render list: head markers, then the flattened transcript.
  // buildRenderRows is the single source of truth for which rows exist.
  // Keyed on the summary's three status facts, not the inputs object (a fresh
  // object each parent render), so the rows only rebuild when the card changes.
  const summaryStatus = summary?.status
  const handBackReason = summary?.handBackReason
  const suspectedCompleteReason = summary?.suspectedCompleteReason
  const rows = useMemo<RenderRow[]>(
    () => buildRenderRows(turns, rotations, {
      loadingOlder, earliestTurn, rewind,
      summary: summaryStatus === undefined ? null : { status: summaryStatus, handBackReason, suspectedCompleteReason },
    }),
    [turns, rotations, loadingOlder, earliestTurn, rewind, summaryStatus, handBackReason, suspectedCompleteReason],
  )
  // Each failed reply's prompt, for its error card's Rewind & retry (D-9).
  const promptOfFailed = useMemo(() => failedPrompts(turns), [turns])

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

  // A search jump in progress (D-11): paging toward its hit, or waiting for the
  // hit's mark to render so a tall turn can be centred on it.
  const pendingJumpRef = useRef<PendingJump | null>(null)
  const markWaitRef = useRef<number | null>(null)
  function cancelJump() {
    pendingJumpRef.current = null
    if (markWaitRef.current !== null) cancelAnimationFrame(markWaitRef.current)
    markWaitRef.current = null
  }

  // The user took over — a user scroll, the Scroll-to-bottom button, a send, a
  // prompt jump: end a jump's hold (`holdJumpedRow`, or a search jump's), so the
  // library's own resize handling is back before the view follows new content
  // down, and abandon a search jump still paging or centring, so it can never yank
  // the view back up when its page lands.
  function releaseJumpHold() {
    virtualizer.shouldAdjustScrollPositionOnItemSizeChange = undefined
    cancelJump()
  }
  useEffect(releaseJumpHold, [followSignal, virtualizer])

  // Alt+↑ / Alt+↓ (the shell's `transcript.promptPrev/Next`): scroll to the
  // previous / next own prompt relative to the topmost VISIBLE row. Unpin
  // first, or the pin would snap the view back to the bottom (`scrollToFn`
  // above, and the spacer observer).
  // The view then holds on the target until the user scrolls (`holdJumpedRow`).
  // Nothing past either end, and no focus moves: the user may be mid-draft.
  // Only the session transcript answers — the assistant drawer's is a different
  // conversation, and the shell only fires these with a session open.
  // ── ⌘F transcript search (D-11) ──
  //
  // The bar's query and results live in the bar (lazy); this view owns what the
  // rest of the transcript needs: whether it is open, the terms to highlight, the
  // turn holding the current hit, and the jump itself.
  const [searchOpen, setSearchOpen] = useState(false)
  const [searchFocus, setSearchFocus] = useState(0)
  const [searchTerms, setSearchTerms] = useState<readonly string[]>([])
  const [current, setCurrent] = useState<SearchCurrent | null>(null)
  // The search kit (lazy): the bar, the highlighters, the jump logic.
  const [kit, setKit] = useState<SearchKitModule | null>(null)
  const restoreFocusRef = useRef<Element | null>(null)
  const searchState = useMemo<TranscriptSearchState | null>(
    () => (searchOpen && kit && searchTerms.length > 0 ? { terms: searchTerms, current, kit: kit.renderKit } : null),
    [searchOpen, kit, searchTerms, current],
  )
  // A new query: no current hit, and a jump toward the old query's hit stops. (The
  // bar reports a query's terms before auto-jumping to its newest hit.)
  const onSearchTerms = useCallback((terms: readonly string[]) => {
    setSearchTerms(terms)
    setCurrent(null)
    cancelJump()
  }, [])

  const runSearch = useCallback((q: string) => fetchTranscriptSearch(sessionId ?? '', q), [sessionId])
  const onSearchError = useCallback((message: string) => { toast.error(message) }, [])

  function ensureKit() {
    if (!kit) void loadSearchKit().then(setKit, () => undefined)
  }

  function openSearch() {
    if (!sessionId) return
    if (!searchOpen) {
      restoreFocusRef.current = document.activeElement
      setSearchOpen(true)
      ensureKit()
    }
    setSearchFocus((n) => n + 1)
  }

  function closeSearch() {
    cancelJump()
    setSearchOpen(false)
    setSearchTerms([])
    setCurrent(null)
    // Back where ⌘F was pressed; from nowhere (the body), to the scroller, so a
    // second ⌘F still searches.
    const back = restoreFocusRef.current
    restoreFocusRef.current = null
    if (back instanceof HTMLElement && back !== document.body && back.isConnected) back.focus({ preventScroll: true })
    else containerRef.current?.focus({ preventScroll: true })
  }

  /** Run the pending search jump one step (`driveJump`, from the kit): a page, a
   *  dead end, or the landing. A page is anchored like a scroll-triggered one, so a
   *  page that lands after the jump was abandoned never shifts the view. */
  function runJump() {
    const p = pendingJumpRef.current
    if (!p || !kit) return
    const done = kit.driveJump(p, {
      earliestTurn: earliestTurn ?? 0,
      loadingOlder: !!loadingOlder,
      rows,
      holds: turnHoldsEngineTurn,
      loadOlder: onLoadOlder && (() => {
        const el = containerRef.current
        if (el) prevScrollHeightRef.current = el.scrollHeight
        onLoadOlder()
      }),
      notify: (message) => toast.info(message),
      land: setCurrent,
      unpin,
      virtualizer,
      scroller: containerRef.current,
      markWait: markWaitRef,
    })
    if (done) pendingJumpRef.current = null
  }

  // The one search jump — the bar's Enter / ↑ / ↓ and the shell's handle both
  // call this. The first jump loads the kit; the effect below runs it then.
  function startJump(req: JumpRequest) {
    cancelJump()
    setCurrent(null)
    pendingJumpRef.current = { ...req, startEarliest: earliestTurn ?? 0, requestedAt: null, sawLoading: false }
    // Unpinned while paging, or the pin would snap the view back to the bottom
    // under every prepended page.
    unpin()
    if (kit) runJump()
    else ensureKit()
  }

  useTranscriptJumpRegistration({
    scrollToTurn: (turnId, ordinal, role) =>
      startJump({ turnId, ordinal, role, rank: 0, dupRank: 0, terms: searchTerms }),
    openSearch,
    jumpPrompt: (dir) => {
      // The user took over: a search jump still paging stops here.
      cancelJump()
      // The topmost visible row from the scroller's LIVE scrollTop — never
      // `virtualizer.range` / `scrollOffset`, which only catch up when the
      // virtualizer handles the `scroll` event a frame after the view moves: a
      // second press inside that frame (key repeat, a quick Alt+↑ then Alt+↓)
      // would target from where the view WAS. `+ SLIVER_PX`: a sliver of the
      // row above, left by a fractional offset after an align-start jump, is
      // not "visible" — counting it would re-target the prompt already on top.
      const el = containerRef.current
      if (!el) return
      const top = virtualizer.getVirtualItemForOffset(el.scrollTop + SLIVER_PX)?.index
      if (top === undefined) return
      const target = ownPromptRowIndex(rows, top, dir)
      if (target === null) return
      unpin()
      virtualizer.shouldAdjustScrollPositionOnItemSizeChange = holdJumpedRow
      virtualizer.scrollToIndex(target, { align: 'start' })
    },
  }, variant === 'session')

  // Update stickiness on scroll; trigger paging when near the top.
  function handleScroll() {
    const el = containerRef.current
    // Skip scrolls we caused ourselves. During entry the whole transcript
    // replays and then keeps growing as markdown/fonts/images lay out late; each
    // pin sets scrollTop to the *then* bottom, but its scroll event arrives after
    // more growth, so without this guard it reads as "user scrolled up" and the
    // auto-pin latches off above the true bottom.
    if (!el || !handleStickScroll()) return
    // A user scroll ends any prompt jump's hold (see `holdJumpedRow`).
    releaseJumpHold()
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

  // Each settled page (or a render with the hit now loaded, or the kit arriving)
  // moves a search jump on. After the anchor effect above, so a page's anchor is
  // applied before the jump scrolls.
  // eslint-disable-next-line react-hooks/exhaustive-deps -- runJump reads this render's props; these are what move it
  useEffect(runJump, [earliestTurn, loadingOlder, rows, kit])

  const virtualRows = virtualizer.getVirtualItems()

  function renderRow(row: RenderRow) {
    switch (row.kind) {
      case 'loading-older':
        return <div className="py-2 text-center text-xs text-ink-muted">Loading earlier history…</div>
      case 'begin':
        return <div className="py-2 text-center text-xs text-ink-muted">— Beginning of session —</div>
      case 'model-switch':
        return <ModelSwitchDivider to={modelName(row.toModel)} />
      case 'rewind':
        return <RewindDividerSlot marker={row.marker} />
      case 'summary':
        // A summary row exists only when `summary` was passed (buildRenderRows).
        return summary ? (
          <TurnSummaryCard
            outcome={row.outcome}
            files={row.files}
            suggestions={summary.suggestions}
            onPrompt={summary.onPrompt}
            onResolveSuspectedComplete={summary.onResolveSuspectedComplete}
            resolvePending={summary.resolvePending}
            onOpenChanges={summary.onOpenChanges}
          />
        ) : null
      case 'rotation':
        return <RotationDivider from={row.rot.from} to={row.rot.to} reason={row.rot.reason} />
      case 'turn':
        return (
          <Turn
            turn={row.turn}
            isLastTurn={row.turn.id === lastTurnId}
            toolDetailMode={toolDetailMode}
            thinkingCollapsed={thinkingCollapsed}
            viewModeEpoch={viewModeEpoch}
            // The open block only ever lives in the last turn; pass null to the
            // rest so their memoized render stays stable across deltas.
            streamingBlockId={row.turn.id === lastTurnId ? streamingBlockId : null}
            onAnswer={onAnswer}
            subAgents={subAgents}
            sessionModel={model}
            variant={variant}
            onAsk={onAsk}
            failedPrompt={promptOfFailed.get(row.turn.id) ?? null}
          />
        )
    }
  }

  return (
    <div
      className="relative flex-1 min-h-0 flex flex-col"
      data-variant={variant}
      // The ⌘F scope (`focusAllowsTranscriptSearch`): the scroller and the bar.
      {...(variant === 'session' ? { [SESSION_TRANSCRIPT_ATTR]: '' } : {})}
    >
      {searchOpen && sessionId && (
        <Suspense fallback={null}>
          <TranscriptSearchBar
            search={runSearch}
            earliestTurn={earliestTurn ?? 0}
            focusSignal={searchFocus}
            onClose={closeSearch}
            onJump={startJump}
            onTerms={onSearchTerms}
            onError={onSearchError}
          />
        </Suspense>
      )}
      <div
        ref={containerRef}
        onScroll={handleScroll}
        // Focusable on click, not by Tab: a click into the transcript then leaves
        // focus here, where ⌘F searches it (D-11). No focus ring — it is never a
        // keyboard stop, and the whole pane would light up on every click.
        tabIndex={-1}
        data-testid="transcript-scroll"
        // overflow-x-hidden is the backstop: an overflow-y-auto box makes
        // overflow-x compute to auto, so any descendant wider than the pane (a
        // stray wide image/embed) would scroll the WHOLE pane sideways. Wrappable
        // content (inline code, links) wraps and code/tables self-scroll via CSS;
        // this clips anything else to the pane instead of scrolling it.
        className="flex-1 overflow-y-auto overflow-x-hidden outline-none"
      >
        {/* The spacer reserves the full virtual height so the scrollbar reflects the
            whole transcript; only the windowed rows are mounted, absolutely
            positioned at their measured offsets. useStickToBottom's ResizeObserver
            watches this element, so its height (=== getTotalSize()) driving the pin is the
            same signal the render-all version got from real content growth. */}
        <TranscriptSearchContext.Provider value={searchState}>
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
        </TranscriptSearchContext.Provider>
      </div>

      {showScrollButton && (
        <ScrollToBottomButton onClick={() => { releaseJumpHold(); scrollToBottom({ smooth: true }) }} />
      )}
    </div>
  )
}
