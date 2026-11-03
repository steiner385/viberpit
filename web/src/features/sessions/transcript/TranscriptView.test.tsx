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
// …and the instance it returns, so a test can pin its visible range and spy on
// scrollToIndex: jsdom lays nothing out, so the real range is not meaningful.
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
    /** Pretend row `top` is the topmost visible one. */
    const showFrom = (top: number) => { v.range = { startIndex: top, endIndex: convo.length - 1 } }
    const jump = (dir: 'prev' | 'next') => act(() => { handle!.current!.jumpPrompt(dir) })
    return { ...view, scrollToIndex, showFrom, jump }
  }

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
