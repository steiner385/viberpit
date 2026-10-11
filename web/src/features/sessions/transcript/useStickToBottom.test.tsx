import { useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { useStickToBottom } from './useStickToBottom'
import { ScrollToBottomButton } from './ScrollToBottomButton'

// A ResizeObserver whose callback the test fires by hand — jsdom has no layout,
// so "content grew" is modelled as: bump scrollHeight, then fire the observer.
let fireResize: () => void = () => {}
class ControlledResizeObserver {
  constructor(cb: () => void) { fireResize = cb }
  observe() {}
  disconnect() {}
  unobserve() {}
}

function Harness({ followSignal }: { followSignal?: unknown }) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const s = useStickToBottom({ scrollRef, contentRef, followSignal })
  return (
    <div style={{ position: 'relative' }}>
      <div ref={scrollRef} data-testid="scroller" onScroll={s.handleScroll}>
        <div ref={contentRef} />
      </div>
      {s.showScrollButton && <ScrollToBottomButton onClick={() => s.scrollToBottom({ smooth: true })} />}
    </div>
  )
}

/** Mutable geometry: `height` of content in a 400px viewport, starting at the bottom. */
function geometry(el: HTMLElement, height = 1000) {
  const g = { height, top: height - 400, smoothCalls: 0 }
  Object.defineProperty(el, 'scrollHeight', { get: () => g.height, configurable: true })
  Object.defineProperty(el, 'clientHeight', { value: 400, configurable: true })
  // Clamped like a browser: setting scrollTop = scrollHeight lands at height - 400.
  Object.defineProperty(el, 'scrollTop', {
    get: () => g.top,
    set: (v: number) => { g.top = Math.max(0, Math.min(v, g.height - 400)) },
    configurable: true,
  })
  // Model a smooth scroll as "started, not yet arrived": record it, move nowhere.
  el.scrollTo = ((o: ScrollToOptions) => {
    if (o.behavior === 'smooth') { g.smoothCalls += 1; return }
    el.scrollTop = o.top ?? 0
  }) as typeof el.scrollTo
  return g
}

/** The programmatic-scroll guard clears on the next frame; a user scroll lands later. */
const nextFrame = () => act(() => new Promise<void>((r) => requestAnimationFrame(() => r())))

function userScrollTo(el: HTMLElement, top: number) {
  el.scrollTop = top
  fireEvent.scroll(el)
}

function grow(g: { height: number }, by: number) {
  g.height += by
  act(() => fireResize())
}

const button = () => screen.queryByRole('button', { name: 'Scroll to bottom' })

function setReducedMotion(reduce: boolean) {
  vi.stubGlobal('matchMedia', (q: string) => ({ matches: reduce && q.includes('reduce'), media: q }))
}

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', ControlledResizeObserver)
  setReducedMotion(false)
})
afterEach(() => { vi.unstubAllGlobals() })

describe('useStickToBottom', () => {
  it('follows growing content down while the user is at the bottom', async () => {
    render(<Harness />)
    const el = screen.getByTestId('scroller')
    const g = geometry(el)
    grow(g, 500) // a streamed reply, an expanded tool card, a late-loading image…
    expect(el.scrollTop).toBe(1100)
    // The pin's own scroll event, delivered after yet more growth, must not unstick.
    g.height += 300
    fireEvent.scroll(el)
    await nextFrame()
    grow(g, 0)
    expect(el.scrollTop).toBe(1400)
    expect(button()).toBeNull()
  })

  it('treats a scroll within the threshold of the bottom as still at the bottom', async () => {
    render(<Harness />)
    const el = screen.getByTestId('scroller')
    const g = geometry(el)
    await nextFrame()
    userScrollTo(el, 550) // 50px above the bottom
    expect(button()).toBeNull()
    grow(g, 200)
    expect(el.scrollTop).toBe(800)
  })

  it('stops following and shows the button once the user scrolls up', async () => {
    render(<Harness />)
    const el = screen.getByTestId('scroller')
    const g = geometry(el)
    await nextFrame()
    userScrollTo(el, 200)
    expect(button()).toBeInTheDocument()
    grow(g, 500)
    expect(el.scrollTop).toBe(200) // reading history is left alone
  })

  it('the button jumps to the bottom and re-enables following', async () => {
    setReducedMotion(true) // instant jump, so the position is checkable immediately
    render(<Harness />)
    const el = screen.getByTestId('scroller')
    const g = geometry(el)
    await nextFrame()
    userScrollTo(el, 200)
    fireEvent.click(button()!)
    expect(el.scrollTop).toBe(600)
    expect(button()).toBeNull()
    grow(g, 300)
    expect(el.scrollTop).toBe(900)
  })

  it('animates the button jump unless reduced motion is on, without unsticking mid-flight', async () => {
    render(<Harness />)
    const el = screen.getByTestId('scroller')
    const g = geometry(el)
    await nextFrame()
    userScrollTo(el, 200)
    fireEvent.click(button()!)
    expect(g.smoothCalls).toBe(1)
    // The animation's intermediate positions are above the bottom by construction.
    userScrollTo(el, 400)
    expect(button()).toBeNull()
    grow(g, 300) // content landing mid-animation is still followed
    expect(el.scrollTop).toBe(900)
  })

  it('a user gesture during the smooth jump hands control back to the user', async () => {
    render(<Harness />)
    const el = screen.getByTestId('scroller')
    geometry(el)
    await nextFrame()
    userScrollTo(el, 200)
    fireEvent.click(button()!)
    fireEvent.wheel(el)
    userScrollTo(el, 150)
    expect(button()).toBeInTheDocument()
  })

  it('a send (followSignal change) re-sticks even when scrolled up; the initial value does not', async () => {
    const { rerender } = render(<Harness followSignal={0} />)
    const el = screen.getByTestId('scroller')
    const g = geometry(el)
    await nextFrame()
    userScrollTo(el, 200)
    rerender(<Harness followSignal={0} />)
    expect(el.scrollTop).toBe(200)
    rerender(<Harness followSignal={1} />)
    expect(el.scrollTop).toBe(600)
    expect(button()).toBeNull()
    grow(g, 400)
    expect(el.scrollTop).toBe(1000)
  })
})
