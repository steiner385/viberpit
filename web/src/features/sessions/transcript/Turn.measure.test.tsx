import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'
import { Turn } from './Turn'
import type { Turn as TurnType } from '@/session/reducer'

// 2026-09-29 Sessions critique (P2, detector `line-length`): in a wide pane
// assistant prose ran ~139 characters a line. A turn carries a reading measure
// so paragraphs stay scannable; it is a no-op wherever the pane is narrower.
describe('Turn — reading measure', () => {
  const turn: TurnType = {
    id: 't1', role: 'assistant',
    blocks: [{ kind: 'text', id: 'b1', text: 'A paragraph of assistant prose.' }],
  }

  it('caps the turn column at 88ch, left-aligned', () => {
    const { container } = render(
      <Turn turn={turn} isLastTurn toolDetailMode="auto" thinkingCollapsed onAnswer={vi.fn()} />,
    )
    const wrapper = container.firstElementChild as HTMLElement
    expect(wrapper.className).toMatch(/max-w-\[88ch\]/)
    expect(wrapper.className).not.toMatch(/mx-auto/)
  })
})
