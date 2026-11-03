import { afterEach, describe, it, expect, vi } from 'vitest'
import { render, fireEvent, screen, act } from '@testing-library/react'
import { elementScroll, type Virtualizer, type VirtualizerOptions } from '@tanstack/react-virtual'
import type { RefObject } from 'react'
import { TranscriptView } from './TranscriptView'
import type { Turn } from '@/session/reducer'
import { SessionGestureProvider, useTranscriptJumpHandle, type TranscriptJump } from '@/shell/sessionGestures'

type Opts = VirtualizerOptions<HTMLDivElement, Element>

// Capture the options TranscriptView hands to useVirtualizer so a test can drive
// the virtualizer's own scroll path (its scrollToFn) without real layout.
let capturedOpts: Opts | null = null
// …and the instance it returns, so a test can read its row measurements and spy
// on scrollToIndex: jsdom lays nothing out, so the scroll position is set by hand.
let capturedVirtualizer: Virtualizer<HTMLDivElement, Element> | null = null
vi.mock('@tanstack/react-virtual', async (importOriginal) => {
  const mod = await importOriginal<typeof import('@tanstack/react-virtual')>()
  return {
    ...mod,
    useVirtualizer: (opts: Opts) => {
      capturedOpts = opts
      capturedVirtualizer = mod.useVirtualizer(opts)
      return capturedVirtualizer
    },
  }
})

const turns: Turn[] = [{ id: 't1', role: 'assistant', blocks: [{ kind: 'text', id: 't1:x', text: 'hi' }] }]
const base = {
  turns, toolDetailMode: 'auto' as const,
  thinkingCollapsed: true, onAnswer: () => {},
}

/**
 * Let the programmatic-scroll guard clear. Mounting the virtualizer measures the
 * rows synchronously and routes its estimate→actual scroll adjustment through
 * TranscriptView's scrollToFn, which flags the current frame as programmatic;
 * a simulated *user* scroll must land in a later frame, as a real one would.
 */
function nextFrame() {
  return new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))
}

/** Give the scroller a fixed geometry: 1000px of content in a 400px viewport. */
function setGeometry(el: HTMLElement, scrollTop: number) {
  Object.defineProperty(el, 'scrollHeight', { value: 1000, configurable: true })
  Object.defineProperty(el, 'clientHeight', { value: 400, configurable: true })
  Object.defineProperty(el, 'scrollTop', { value: scrollTop, writable: true, configurable: true })
  // jsdom has no Element.scrollTo; model the browser: move, then fire a scroll event.
  el.scrollTo = ((o: ScrollToOptions) => { el.scrollTop = o.top ?? 0; fireEvent.scroll(el) }) as typeof el.scrollTo
}

/** A minimal Virtualizer instance for scrollToFn — only scrollElement/options are read. */
function fakeInstance(el: HTMLDivElement) {
  return { scrollElement: el, options: { horizontal: false } } as unknown as Virtualizer<HTMLDivElement, Element>
}

// The virtualizer arms a 150 ms "scrolling reset" timer on every scroll
// (`isScrollingResetDelay`); a scroll fired by the last test in this file would
// let that timer fire after vitest has torn down jsdom, and react-virtual's
// notify then dispatches into React with no `window` ("Unhandled Error:
// ReferenceError: window is not defined" — the CI flake first seen on
// finish-line row 6). Let it fire while the environment still exists.
afterEach(() => new Promise<void>((resolve) => setTimeout(resolve, 200)))

describe('TranscriptView auto-pin vs virtualizer scroll adjustments', () => {
  // On entry every row above the viewport measures for the first time, and the
  // virtualizer compensates each estimate→actual delta by moving scrollTop
  // itself (virtual-core resizeItem → applyScrollAdjustment → scrollToFn). That
  // scroll is not a user gesture; if the near-bottom check sees it, the auto-pin
  // latches off a few hundred px above the true bottom and the session opens
  // "a little bit above the bottom" with the Scroll-to-bottom button showing.
  it('does not latch auto-pin off when the virtualizer adjusts scrollTop for a remeasured row', () => {
    render(<TranscriptView {...base} />)
    const scroller = screen.getByTestId('transcript-scroll') as HTMLDivElement
    setGeometry(scroller, 600) // at the bottom (1000 - 400)

    // Rows above shrank by 200px in total: the virtualizer pulls scrollTop up by 200
    // through whichever scrollToFn is in force (the library default when none is passed).
    const scrollToFn = capturedOpts!.scrollToFn ?? elementScroll
    act(() => {
      scrollToFn(600, { adjustments: -200, behavior: undefined }, fakeInstance(scroller))
    })

    expect(screen.queryByRole('button', { name: 'Scroll to bottom' })).toBeNull()
    // While pinned, the bottom — not the anchored row — is the invariant to keep.
    expect(scroller.scrollTop).toBeGreaterThanOrEqual(600)
  })

  it('still shows the Scroll-to-bottom button after a genuine user scroll away from the bottom', async () => {
    render(<TranscriptView {...base} />)
    const scroller = screen.getByTestId('transcript-scroll') as HTMLDivElement
    setGeometry(scroller, 400) // 200px above the bottom, past the 80px threshold
    await nextFrame()

    fireEvent.scroll(scroller)

    expect(screen.getByRole('button', { name: 'Scroll to bottom' })).toBeInTheDocument()
  })
})

describe('TranscriptView followSignal', () => {
  it('a local send re-sticks a transcript the user had scrolled up in', async () => {
    const { rerender } = render(<TranscriptView {...base} followSignal={0} />)
    const scroller = screen.getByTestId('transcript-scroll') as HTMLDivElement
    setGeometry(scroller, 600)
    await act(nextFrame)
    scroller.scrollTop = 100
    fireEvent.scroll(scroller)
    expect(screen.getByRole('button', { name: 'Scroll to bottom' })).toBeInTheDocument()
    rerender(<TranscriptView {...base} followSignal={1} />)
    expect(screen.queryByRole('button', { name: 'Scroll to bottom' })).toBeNull()
    expect(scroller.scrollTop).toBe(1000)
  })
})

// Note: stick-to-bottom and windowing behavior depend on real layout, which
// jsdom does not compute and which is now shared with the @tanstack/react-virtual
// virtualizer's own ResizeObservers. Those behaviors are covered in
// e2e/transcript-virtualization.spec.ts. The tests below assert only the
// layout-independent scroll wiring (paging callback + overflow clipping).

describe('TranscriptView scrollback', () => {
  it('calls onLoadOlder when scrolled to top and more history exists', async () => {
    const onLoadOlder = vi.fn()
    render(<TranscriptView {...base} earliestTurn={5} loadingOlder={false} onLoadOlder={onLoadOlder} />)
    const scroller = screen.getByTestId('transcript-scroll')
    Object.defineProperty(scroller, 'scrollTop', { value: 0, writable: true })
    await nextFrame()
    fireEvent.scroll(scroller)
    expect(onLoadOlder).toHaveBeenCalled()
  })

  it('does not call onLoadOlder at session start', () => {
    const onLoadOlder = vi.fn()
    render(<TranscriptView {...base} earliestTurn={0} loadingOlder={false} onLoadOlder={onLoadOlder} />)
    const scroller = screen.getByTestId('transcript-scroll')
    Object.defineProperty(scroller, 'scrollTop', { value: 0, writable: true })
    fireEvent.scroll(scroller)
    expect(onLoadOlder).not.toHaveBeenCalled()
  })

  it('clips the pane horizontally so wide content cannot scroll it sideways', () => {
    // The scroll container is overflow-y-auto, which makes overflow-x compute to
    // auto — so without an explicit clip, any descendant wider than the pane (a
    // long unbreakable token, a wide image) drags the whole transcript sideways.
    // overflow-x-hidden is the backstop; wrappable content wraps via CSS.
    render(<TranscriptView {...base} />)
    const scroller = screen.getByTestId('transcript-scroll')
    expect(scroller.className).toContain('overflow-x-hidden')
    expect(scroller.className).toContain('overflow-y-auto')
  })

  // The beginning-of-session marker is a virtualized row; its presence logic is
  // unit-tested in transcriptRows.test.ts (buildRenderRows) and its rendering is
  // covered by e2e/transcript-virtualization.spec.ts.
})

// ── Alt+↑ / Alt+↓: jump between the user's own prompts (D-10) ────────────────

describe('TranscriptView jumpPrompt', () => {
  const text = (id: string) => [{ kind: 'text' as const, id: `${id}:x`, text: id }]
  // Row index = turn index: no head markers (no earliestTurn), no dividers.
  const convo: Turn[] = [
    { id: 'p1', role: 'user', blocks: text('p1') }, // 0 own prompt
    { id: 'a1', role: 'assistant', blocks: text('a1') }, // 1
    { id: 'n1', role: 'user', blocks: text('n1'), origin: { kind: 'autopilot' } }, // 2 nudge — skipped
    { id: 'a2', role: 'assistant', blocks: text('a2') }, // 3
    { id: 'p2', role: 'user', blocks: text('p2') }, // 4 own prompt
    { id: 'a3', role: 'assistant', blocks: text('a3') }, // 5
  ]

  let handle: RefObject<TranscriptJump | null> | null = null
  function Probe() {
    handle = useTranscriptJumpHandle()
    return null
  }

  function renderConvo() {
    const view = render(
      <SessionGestureProvider>
        <TranscriptView {...base} turns={convo} />
        <Probe />
      </SessionGestureProvider>,
    )
    const v = capturedVirtualizer!
    const scrollToIndex = vi.spyOn(v, 'scrollToIndex').mockImplementation(() => {})
    const scroller = screen.getByTestId('transcript-scroll')
    /** Put the scroller's LIVE scroll position at `top` px (jsdom lays nothing out). */
    const scrollTo = (top: number) =>
      Object.defineProperty(scroller, 'scrollTop', { value: top, writable: true, configurable: true })
    /** Scroll so row `index` starts exactly at the top edge — the topmost visible row. */
    const showFrom = (index: number) => scrollTo(v.measurementsCache[index].start)
    const jump = (dir: 'prev' | 'next') => act(() => { handle!.current!.jumpPrompt(dir) })
    return { ...view, scrollToIndex, scrollTo, showFrom, jump }
  }

  // The root cause of the 9/40 keyboard-nav e2e failure: `virtualizer.range`
  // and `scrollOffset` only catch up when the virtualizer handles the `scroll`
  // event, a frame AFTER scrollTop moves. A key pressed inside that frame — key
  // repeat, a quick double press — targeted from where the view WAS. The jump
  // reads the scroller's live scrollTop instead.
  it('targets from the LIVE scroll position, not the virtualizer\'s not-yet-updated range', () => {
    const { scrollToIndex, showFrom, jump } = renderConvo()
    const v = capturedVirtualizer!
    showFrom(0) // the view is at p1…
    v.range = { startIndex: 4, endIndex: 5 } // …but the range still says p2 (stale)
    v.scrollOffset = v.measurementsCache[4].start
    jump('next')
    expect(scrollToIndex).toHaveBeenLastCalledWith(4, { align: 'start' })
  })

  it('Alt+↑ goes to the previous own prompt above the topmost visible row', () => {
    const { scrollToIndex, showFrom, jump } = renderConvo()
    showFrom(5)
    jump('prev')
    expect(scrollToIndex).toHaveBeenLastCalledWith(4, { align: 'start' })
  })

  it('skips nudges — a user turn with an origin is not the user\'s own prompt', () => {
    const { scrollToIndex, showFrom, jump } = renderConvo()
    showFrom(4)
    jump('prev')
    expect(scrollToIndex).toHaveBeenLastCalledWith(0, { align: 'start' })
    showFrom(0)
    jump('next')
    expect(scrollToIndex).toHaveBeenLastCalledWith(4, { align: 'start' })
  })

  it('does nothing at either end', () => {
    const { scrollToIndex, showFrom, jump } = renderConvo()
    showFrom(0)
    jump('prev')
    showFrom(4)
    jump('next')
    expect(scrollToIndex).not.toHaveBeenCalled()
  })

  it('unpins stick-to-bottom, so the transcript stays where it jumped', () => {
    const { showFrom, jump } = renderConvo()
    expect(screen.queryByRole('button', { name: 'Scroll to bottom' })).toBeNull()
    showFrom(5)
    jump('prev')
    expect(screen.getByRole('button', { name: 'Scroll to bottom' })).toBeInTheDocument()
  })

  // virtual-core skips scroll compensation for a RE-measured row above the
  // viewport while the scroll direction is backward — right for a user scrolling
  // up, wrong for a jump: a reply that settles a few hundred px shorter after the
  // jump would drag the prompt off the top (seen in e2e, 591px). The jump holds
  // the view by compensating every above-viewport resize until the user scrolls.
  it('holds the jumped-to row in place while rows above it settle, until the user scrolls', async () => {
    const { showFrom, jump } = renderConvo()
    const v = capturedVirtualizer!
    expect(v.shouldAdjustScrollPositionOnItemSizeChange).toBeUndefined()
    showFrom(5)
    jump('prev')
    const hold = v.shouldAdjustScrollPositionOnItemSizeChange
    expect(hold).toBeDefined()
    v.scrollOffset = 500
    const item = (start: number) => ({ index: 0, key: 'k', start, size: 100, end: start + 100, lane: 0 })
    expect(hold!(item(100), -50, v)).toBe(true) // above the viewport: compensate
    expect(hold!(item(600), -50, v)).toBe(false) // in or below it: leave alone

    // A genuine user scroll hands resize handling back to the library.
    const scroller = screen.getByTestId('transcript-scroll')
    await act(nextFrame)
    fireEvent.scroll(scroller)
    expect(v.shouldAdjustScrollPositionOnItemSizeChange).toBeUndefined()
  })

  // After an align-start jump the row above can keep a sub-pixel sliver in view
  // (fractional scroll offsets), and the range then reports IT as topmost — so
  // the next Alt+↓ re-targeted the prompt already at the top (seen in e2e).
  it('a sub-pixel sliver of the row above does not count as the topmost visible row', () => {
    const { scrollToIndex, scrollTo, jump } = renderConvo()
    const v = capturedVirtualizer!
    const a2 = v.measurementsCache[3]
    scrollTo(a2.end - 0.5) // a2 "visible", but only its last half-pixel…
    jump('next') // …so p2 (4) is the top, and there is no own prompt after it
    expect(scrollToIndex).not.toHaveBeenCalled()
    scrollTo(a2.start + 10) // a2 genuinely in view: p2 is next
    jump('next')
    expect(scrollToIndex).toHaveBeenLastCalledWith(4, { align: 'start' })
  })

  it('re-pinning ends the hold — the Scroll-to-bottom button', () => {
    const { showFrom, jump } = renderConvo()
    const v = capturedVirtualizer!
    showFrom(5)
    jump('prev')
    expect(v.shouldAdjustScrollPositionOnItemSizeChange).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Scroll to bottom' }))
    expect(v.shouldAdjustScrollPositionOnItemSizeChange).toBeUndefined()
  })

  it('re-pinning ends the hold — a send (followSignal)', () => {
    const view = render(
      <SessionGestureProvider>
        <TranscriptView {...base} turns={convo} followSignal={0} />
        <Probe />
      </SessionGestureProvider>,
    )
    const v = capturedVirtualizer!
    vi.spyOn(v, 'scrollToIndex').mockImplementation(() => {})
    Object.defineProperty(screen.getByTestId('transcript-scroll'), 'scrollTop',
      { value: v.measurementsCache[5].start, writable: true, configurable: true })
    act(() => { handle!.current!.jumpPrompt('prev') })
    expect(v.shouldAdjustScrollPositionOnItemSizeChange).toBeDefined()
    view.rerender(
      <SessionGestureProvider>
        <TranscriptView {...base} turns={convo} followSignal={1} />
        <Probe />
      </SessionGestureProvider>,
    )
    expect(capturedVirtualizer!.shouldAdjustScrollPositionOnItemSizeChange).toBeUndefined()
  })

  it('focuses nothing — focus stays where the key was pressed', () => {
    const { showFrom, jump } = renderConvo()
    const before = document.activeElement
    showFrom(5)
    jump('prev')
    expect(document.activeElement).toBe(before)
  })

  it('only the session transcript registers — the assistant drawer\'s never answers Alt+↑', () => {
    // User turns only: the drawer's assistant replies need its runtime (feedback).
    render(
      <SessionGestureProvider>
        <TranscriptView {...base} turns={convo.filter((t) => t.role === 'user')} variant="assistant" />
        <Probe />
      </SessionGestureProvider>,
    )
    expect(handle!.current).toBeNull()
  })

  it('unregisters on unmount, so a closed session leaves nothing to jump', () => {
    const { unmount } = renderConvo()
    expect(handle!.current).not.toBeNull()
    const ref = handle!
    unmount()
    expect(ref.current).toBeNull()
  })
})

// ── ⌘F: jump to a search hit, paging older history in first (D-11) ──────────

describe('TranscriptView scrollToTurn', () => {
  const text = (id: string) => [{ kind: 'text' as const, id: `${id}:x`, text: id }]
  /** An exchange per ordinal: the prompt `u<n>`, then the reply `a<n>` holding ordinal n. */
  const exchanges = (from: number, to: number): Turn[] => {
    const out: Turn[] = []
    for (let n = from; n <= to; n++) {
      out.push({ id: `u${n}`, role: 'user', blocks: text(`u${n}`) })
      out.push({ id: `a${n}`, role: 'assistant', blocks: text(`a${n}`), ordinal: n })
    }
    return out
  }

  let handle: RefObject<TranscriptJump | null> | null = null
  function Probe() {
    handle = useTranscriptJumpHandle()
    return null
  }

  type ViewProps = Partial<React.ComponentProps<typeof TranscriptView>>
  function renderPaged(props: ViewProps) {
    const onLoadOlder = vi.fn()
    const tree = (p: ViewProps) => (
      <SessionGestureProvider>
        <TranscriptView {...base} sessionId="s1" onLoadOlder={onLoadOlder} loadingOlder={false} {...p} />
        <Probe />
      </SessionGestureProvider>
    )
    const view = render(tree(props))
    const spy = () => vi.spyOn(capturedVirtualizer!, 'scrollToIndex').mockImplementation(() => {})
    let scrollToIndex = spy()
    const rerender = (p: ViewProps) => {
      view.rerender(tree({ ...props, ...p }))
      // A rerender keeps the same virtualizer instance; re-spy only if it changed.
      if (!vi.isMockFunction(capturedVirtualizer!.scrollToIndex)) scrollToIndex = spy()
    }
    const jump = (turnId: string, ordinal: number, role: 'user' | 'assistant' = 'assistant') =>
      act(() => { handle!.current!.scrollToTurn(turnId, ordinal, role) })
    return { ...view, onLoadOlder, rerender, jump, scrollToIndex: () => scrollToIndex }
  }

  /** The index `rows` puts the turn with id `id` at, for these props. */
  const rowOf = (id: string) => {
    const { count, getItemKey } = capturedVirtualizer!.options
    return Array.from({ length: count }, (_, i) => getItemKey(i)).indexOf(`turn-${id}`)
  }

  it('centres a loaded hit\'s row and holds it there, unpinned', () => {
    const t = renderPaged({ turns: exchanges(0, 5), earliestTurn: 0 })
    t.jump('a2', 2)
    expect(t.scrollToIndex()).toHaveBeenLastCalledWith(rowOf('a2'), { align: 'center' })
    expect(capturedVirtualizer!.shouldAdjustScrollPositionOnItemSizeChange).toBeDefined()
    expect(screen.getByRole('button', { name: 'Scroll to bottom' })).toBeInTheDocument()
    expect(t.onLoadOlder).not.toHaveBeenCalled()
  })

  it('holds every row above the hit — not just above the viewport — so a centred hit stays put', () => {
    const t = renderPaged({ turns: exchanges(0, 5), earliestTurn: 0 })
    t.jump('a3', 3)
    const v = capturedVirtualizer!
    const hold = v.shouldAdjustScrollPositionOnItemSizeChange!
    const target = rowOf('a3')
    v.scrollOffset = 0 // every row is "in view"…
    const item = (index: number) => ({ index, key: 'k', start: index * 100, size: 100, end: index * 100 + 100, lane: 0 })
    expect(hold(item(target - 1), -50, v)).toBe(true) // …yet a row above the hit is compensated
    expect(hold(item(target), -50, v)).toBe(false)
    expect(hold(item(target + 1), -50, v)).toBe(false)
  })

  it('a user hit lands on the prompt itself', () => {
    const t = renderPaged({ turns: exchanges(0, 5), earliestTurn: 0 })
    t.jump('u3', 3, 'user')
    expect(t.scrollToIndex()).toHaveBeenLastCalledWith(rowOf('u3'), { align: 'center' })
  })

  it('falls back to the ordinal when the hit\'s id is not a rendered turn', () => {
    const t = renderPaged({ turns: exchanges(0, 5), earliestTurn: 0 })
    t.jump('renamed', 4)
    expect(t.scrollToIndex()).toHaveBeenLastCalledWith(rowOf('a4'), { align: 'center' })
  })

  it('pages older history in through onLoadOlder until the hit is loaded, then scrolls to it', () => {
    const t = renderPaged({ turns: exchanges(10, 12), earliestTurn: 10 })
    t.jump('a3', 3)
    expect(t.onLoadOlder).toHaveBeenCalledTimes(1)
    expect(t.scrollToIndex()).not.toHaveBeenCalled()

    // The first page is in flight, then lands: 6..12 — still short of 3.
    t.rerender({ loadingOlder: true })
    expect(t.onLoadOlder).toHaveBeenCalledTimes(1)
    t.rerender({ turns: exchanges(6, 12), earliestTurn: 6, loadingOlder: false })
    expect(t.onLoadOlder).toHaveBeenCalledTimes(2)
    expect(t.scrollToIndex()).not.toHaveBeenCalled()

    // The second page reaches it.
    t.rerender({ loadingOlder: true })
    t.rerender({ turns: exchanges(2, 12), earliestTurn: 2, loadingOlder: false })
    expect(t.onLoadOlder).toHaveBeenCalledTimes(2)
    expect(t.scrollToIndex()).toHaveBeenLastCalledWith(rowOf('a3'), { align: 'center' })
  })

  it('stops paging when a page brings nothing (a failed fetch) rather than looping', () => {
    const t = renderPaged({ turns: exchanges(10, 12), earliestTurn: 10 })
    t.jump('a3', 3)
    t.rerender({ loadingOlder: true })
    t.rerender({ loadingOlder: false }) // the fetch failed: earliestTurn unchanged
    t.rerender({ turns: [...exchanges(10, 12)] }) // and any later render…
    expect(t.onLoadOlder).toHaveBeenCalledTimes(1) // …does not ask again
    expect(t.scrollToIndex()).not.toHaveBeenCalled()
  })

  it('never pages more than 2000 turns back', () => {
    const t = renderPaged({ turns: exchanges(2500, 2502), earliestTurn: 2500 })
    t.jump('a5', 5)
    expect(t.onLoadOlder).not.toHaveBeenCalled()
    expect(t.scrollToIndex()).not.toHaveBeenCalled()
  })

  it('a hit at exactly 2000 turns back still pages', () => {
    const t = renderPaged({ turns: exchanges(2005, 2006), earliestTurn: 2005 })
    t.jump('a5', 5)
    expect(t.onLoadOlder).toHaveBeenCalledTimes(1)
  })
})

describe('TranscriptView search pane', () => {
  it('the session scroller takes focus on a click (tabIndex -1) without an outline, inside the searchable pane', () => {
    render(<TranscriptView {...base} />)
    const scroller = screen.getByTestId('transcript-scroll')
    expect(scroller).toHaveAttribute('tabindex', '-1')
    expect(scroller.className).toContain('outline-none')
    expect(scroller.closest('[data-session-transcript]')).not.toBeNull()
  })

  it('the assistant drawer\'s transcript is not the searchable pane', () => {
    render(<TranscriptView {...base} turns={[{ id: 'u', role: 'user', blocks: [{ kind: 'text', id: 'u:x', text: 'u' }] }]} variant="assistant" />)
    expect(screen.getByTestId('transcript-scroll').closest('[data-session-transcript]')).toBeNull()
  })
})
