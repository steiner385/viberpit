import { afterEach, describe, it, expect, vi } from 'vitest'
import { render, fireEvent, screen, act } from '@testing-library/react'
import { elementScroll, type Virtualizer, type VirtualizerOptions } from '@tanstack/react-virtual'
import { TranscriptView } from './TranscriptView'
import type { Turn } from '@/session/reducer'

type Opts = VirtualizerOptions<HTMLDivElement, Element>

// Capture the options TranscriptView hands to useVirtualizer so a test can drive
// the virtualizer's own scroll path (its scrollToFn) without real layout.
let capturedOpts: Opts | null = null
vi.mock('@tanstack/react-virtual', async (importOriginal) => {
  const mod = await importOriginal<typeof import('@tanstack/react-virtual')>()
  return {
    ...mod,
    useVirtualizer: (opts: Opts) => {
      capturedOpts = opts
      return mod.useVirtualizer(opts)
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
