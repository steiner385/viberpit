import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'
import { Turn } from './Turn'
import type { Turn as TurnType } from '@/session/reducer'

// 2026-09-29 Sessions critique (P2, detector `line-length`): in a wide pane
// assistant prose ran ~139 characters a line. Assistant PROSE carries a reading
// measure so paragraphs stay scannable; it is a no-op wherever the pane is
// narrower. The measure is on the prose block, not the turn column: when it sat
// on the column (#2529/#2535) every tool card, code block and raw JSON result
// was squeezed into ~574px at the left of a 1,300px pane (2026-09-30 report).
describe('Turn — reading measure', () => {
  const MEASURE = /max-w-\[70ch\]/
  const ANY_CAP = /max-w-/

  const proseTurn: TurnType = {
    id: 't1', role: 'assistant',
    blocks: [{ kind: 'text', id: 'b1', text: 'A paragraph of assistant prose.' }],
  }

  const toolTurn: TurnType = {
    id: 't2', role: 'assistant',
    blocks: [{
      kind: 'tool', id: 'b2', toolId: 'tu1', name: 'Bash', toolKind: 'bash',
      input: { command: 'ls' }, result: { ok: true, payload: { output: 'x'.repeat(400) } },
    }],
  }

  function renderTurn(turn: TurnType) {
    return render(
      <Turn turn={turn} isLastTurn toolDetailMode="auto" thinkingCollapsed onAnswer={vi.fn()} />,
    )
  }

  it('caps assistant prose at 70ch (≈ 80–85 real characters), left-aligned', () => {
    // 88ch still measured ~104 characters: `ch` is the width of "0", running
    // text is narrower. 70ch lands in the craft-floor band.
    const { container, getByText } = renderTurn(proseTurn)
    const prose = getByText('A paragraph of assistant prose.')
    const measure = prose.closest('[class*="max-w-"]') as HTMLElement | null
    expect(measure, 'prose sits inside a measured wrapper').not.toBeNull()
    expect(measure!.className).toMatch(MEASURE)
    expect(measure!.className).not.toMatch(/mx-auto/)
    expect(container.contains(measure)).toBe(true)
  })

  it('does not cap the turn column itself — tool cards span the pane', () => {
    const { container } = renderTurn(toolTurn)
    const wrapper = container.firstElementChild as HTMLElement
    expect(wrapper.className).not.toMatch(ANY_CAP)
    // Nothing between the turn column and the tool card narrows it either.
    const capped = container.querySelector('[class*="max-w-"]')
    expect(capped).toBeNull()
  })
})
