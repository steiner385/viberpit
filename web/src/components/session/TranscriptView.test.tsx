import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { TranscriptView } from './TranscriptView'
import type { Turn } from '@/session/reducer'

const turns: Turn[] = [{ id: 't1', role: 'assistant', blocks: [{ kind: 'text', id: 't1:x', text: 'hi' }] }]
const base = {
  turns, permissionMode: 'interactive', toolDetailMode: 'auto' as const,
  thinkingCollapsed: true, onDecision: () => {}, onAnswer: () => {},
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
