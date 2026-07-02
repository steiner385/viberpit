import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { TranscriptView } from './TranscriptView'
import type { Turn } from '@/session/reducer'

const turns: Turn[] = [{ id: 't1', role: 'assistant', blocks: [{ kind: 'text', id: 't1:x', text: 'hi' }] }]
const base = {
  turns, toolDetailMode: 'auto' as const,
  thinkingCollapsed: true, onAnswer: () => {},
}

// Note: stick-to-bottom and windowing behavior depend on real layout, which
// jsdom does not compute and which is now shared with the @tanstack/react-virtual
// virtualizer's own ResizeObservers. Those behaviors are covered in
// e2e/transcript-virtualization.spec.ts. The tests below assert only the
// layout-independent scroll wiring (paging callback + overflow clipping).

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

  it('clips the pane horizontally so wide content cannot scroll it sideways', () => {
    // The scroll container is overflow-y-auto, which makes overflow-x compute to
    // auto — so without an explicit clip, any descendant wider than the pane (a
    // long unbreakable token, a wide image) drags the whole transcript sideways.
    // overflow-x-hidden is the backstop; wrappable content wraps via CSS.
    const { container } = render(<TranscriptView {...base} />)
    const scroller = container.firstChild as HTMLElement
    expect(scroller.className).toContain('overflow-x-hidden')
    expect(scroller.className).toContain('overflow-y-auto')
  })

  // The beginning-of-session marker is a virtualized row; its presence logic is
  // unit-tested in transcriptRows.test.ts (buildRenderRows) and its rendering is
  // covered by e2e/transcript-virtualization.spec.ts.
})
