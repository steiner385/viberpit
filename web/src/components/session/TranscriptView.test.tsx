import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { TranscriptView } from './TranscriptView'
import type { Turn } from '@/session/reducer'

const turns: Turn[] = [{ id: 't1', role: 'assistant', blocks: [{ kind: 'text', id: 't1:x', text: 'hi' }] }]
const base = {
  turns, permissionMode: 'interactive', toolDetailMode: 'auto' as const,
  thinkingCollapsed: true, onDecision: () => {}, onAnswer: () => {},
}

/**
 * Install a ResizeObserver mock that captures the callback so a test can drive
 * the content-resize "pin to bottom" path deterministically (jsdom does no
 * layout, so the real observer never fires). Returns a fn that invokes the
 * captured callback, plus a restore fn.
 */
function captureResizeObserver() {
  const original = globalThis.ResizeObserver
  let cb: (() => void) | null = null
  class MockRO {
    constructor(fn: () => void) { cb = fn }
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = MockRO
  return {
    fire: () => cb?.(),
    restore: () => { (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = original },
  }
}

/**
 * Make requestAnimationFrame flushable on demand so a test can advance to the
 * "next frame" deterministically. Returns a flush fn and a restore fn.
 */
function controllableRaf() {
  const original = globalThis.requestAnimationFrame
  let queued: FrameRequestCallback[] = []
  globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { queued.push(cb); return queued.length }) as typeof requestAnimationFrame
  return {
    flush: () => { const q = queued; queued = []; q.forEach((cb) => cb(0)) },
    restore: () => { globalThis.requestAnimationFrame = original },
  }
}

/** Give a scroll container fake, mutable layout metrics jsdom doesn't compute. */
function fakeMetrics(el: HTMLElement, { clientHeight, scrollHeight }: { clientHeight: number; scrollHeight: number }) {
  let top = 0
  let height = scrollHeight
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => clientHeight })
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => height })
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => top,
    // Mirror the browser: scrollTop is clamped to the max scroll offset.
    set: (v: number) => { top = Math.min(v, Math.max(0, height - clientHeight)) },
  })
  return {
    get scrollTop() { return top },
    grow: (to: number) => { height = to },
  }
}

describe('TranscriptView scrollback', () => {
  it('calls onLoadOlder when scrolled to top and more history exists', () => {
    const onLoadOlder = vi.fn()
    const { container } = render(
      <TranscriptView {...base} earliestTurn={5} loadingOlder={false} onLoadOlder={onLoadOlder} />)
    const scroller = container.firstChild as HTMLElement
    Object.defineProperty(scroller, 'scrollTop', { value: 0, writable: true })
    fireEvent.scroll(scroller)
    expect(onLoadOlder).toHaveBeenCalled()
  })

  it('does not call onLoadOlder at session start', () => {
    const onLoadOlder = vi.fn()
    const { container } = render(
      <TranscriptView {...base} earliestTurn={0} loadingOlder={false} onLoadOlder={onLoadOlder} />)
    const scroller = container.firstChild as HTMLElement
    Object.defineProperty(scroller, 'scrollTop', { value: 0, writable: true })
    fireEvent.scroll(scroller)
    expect(onLoadOlder).not.toHaveBeenCalled()
  })

  it('shows the beginning-of-session marker at the start', () => {
    const { getByText } = render(<TranscriptView {...base} earliestTurn={0} />)
    expect(getByText(/beginning of session/i)).toBeTruthy()
  })

  it('does not show the beginning-of-session marker in read-only views (no earliestTurn prop)', () => {
    const { queryByText } = render(<TranscriptView {...base} />)
    expect(queryByText(/beginning of session/i)).toBeNull()
  })
})

describe('TranscriptView stick-to-bottom on entry', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('keeps pinning to the true bottom when content grows past the threshold mid-replay', () => {
    // Reproduces the entry race: the pin sets scrollTop to the *current* bottom,
    // but async markdown/font/image layout then grows the content further before
    // the pin's own scroll event is delivered. That scroll event must NOT be read
    // as a user scroll-up, or the auto-pin latches off above the true bottom.
    const ro = captureResizeObserver()
    try {
      const { container } = render(<TranscriptView {...base} earliestTurn={0} />)
      const scroller = container.firstChild as HTMLElement
      const m = fakeMetrics(scroller, { clientHeight: 100, scrollHeight: 200 })

      // Initial pin → lands at the bottom (max offset = 200 - 100).
      ro.fire()
      expect(m.scrollTop).toBe(100)

      // Content grows asynchronously; the pin's scroll event arrives now, when
      // scrollHeight (400) is already taller than the scrollTop we set (100).
      m.grow(400)
      fireEvent.scroll(scroller)

      // The next resize must STILL pin to the new bottom (max offset = 400 - 100).
      ro.fire()
      expect(m.scrollTop).toBe(300)
    } finally {
      ro.restore()
    }
  })

  it('stops auto-pinning once the user genuinely scrolls up to read history', () => {
    const ro = captureResizeObserver()
    const raf = controllableRaf()
    try {
      const { container } = render(<TranscriptView {...base} earliestTurn={0} />)
      const scroller = container.firstChild as HTMLElement
      const m = fakeMetrics(scroller, { clientHeight: 100, scrollHeight: 1000 })

      ro.fire()
      expect(m.scrollTop).toBe(900) // pinned to bottom
      raf.flush() // advance past the entry pin's frame so its programmatic flag clears

      // A real user scroll-up (not programmatic) well past the near-bottom threshold.
      scroller.scrollTop = 100
      fireEvent.scroll(scroller)

      // Subsequent content growth must NOT yank them back to the bottom.
      m.grow(1200)
      ro.fire()
      expect(m.scrollTop).toBe(100)
    } finally {
      ro.restore()
      raf.restore()
    }
  })
})
