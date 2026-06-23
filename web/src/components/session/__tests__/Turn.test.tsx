import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Turn } from '../Turn'
import type { Turn as TurnType, Block } from '@/session/reducer'

function tool(id: string): Block {
  return {
    kind: 'tool', id, toolId: id, name: 'Read', toolKind: 'read',
    input: { file_path: `/f/${id}` }, result: { ok: true, payload: { content: 'x' } },
  }
}

function makeTurn(blocks: Block[]): TurnType {
  return { id: 'turn-1', role: 'assistant', blocks }
}

const noop = () => {}

describe('Turn tool grouping', () => {
  it('renders one line per completed tool — no rollup — when NOT the last turn', () => {
    render(
      <Turn
        turn={makeTurn([tool('a'), tool('b'), tool('c'), tool('d')])}
        isLastTurn={false}
        toolDetailMode="auto"
        thinkingCollapsed={true}
        onAnswer={noop}
      />,
    )
    expect(screen.queryByRole('button', { name: /tool group/i })).toBeNull()
    const rows = screen.getAllByRole('button', { name: /tool details/i })
    expect(rows).toHaveLength(4)
    // Auto + not live tail → every result stays collapsed.
    for (const row of rows) expect(row).toHaveAttribute('aria-expanded', 'false')
  })

  it('keeps the tool run expanded when this IS the last turn (live tail)', () => {
    render(
      <Turn
        turn={makeTurn([tool('a'), tool('b'), tool('c'), tool('d')])}
        isLastTurn
        toolDetailMode="auto"
        thinkingCollapsed={true}
        onAnswer={noop}
      />,
    )
    expect(screen.queryByRole('button', { name: /tool group/i })).toBeNull()
    expect(screen.getAllByRole('button', { name: /tool details/i })).toHaveLength(4)
  })

  it('renders text blocks between separate tool groups in order', () => {
    const blocks: Block[] = [tool('a'), { kind: 'text', id: 't', text: 'hello world' }, tool('b'), tool('c')]
    render(
      <Turn
        turn={makeTurn(blocks)}
        isLastTurn={false}
        toolDetailMode="auto"
        thinkingCollapsed={true}
        onAnswer={noop}
      />,
    )
    expect(screen.getByText('hello world')).toBeInTheDocument()
  })
})

describe('Turn rendering edge cases', () => {
  it('renders nothing for a turn with no blocks (no stray padded gap)', () => {
    const { container } = render(
      <Turn
        turn={makeTurn([])}
        isLastTurn
        toolDetailMode="auto"
        thinkingCollapsed={true}
        onAnswer={noop}
      />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('renders the open assistant block as raw text while it is streaming', () => {
    const blocks: Block[] = [{ kind: 'text', id: 't1', text: '**bold**' }]
    render(
      <Turn
        turn={makeTurn(blocks)}
        isLastTurn
        toolDetailMode="auto"
        thinkingCollapsed={true}
        streamingBlockId="t1"
        onAnswer={noop}
      />,
    )
    expect(screen.getByText('**bold**')).toBeInTheDocument()
    expect(document.querySelector('strong')).toBeNull()
  })

  it('renders a finalized (non-streaming) assistant block as markdown', () => {
    const blocks: Block[] = [{ kind: 'text', id: 't1', text: '**bold**' }]
    render(
      <Turn
        turn={makeTurn(blocks)}
        isLastTurn
        toolDetailMode="auto"
        thinkingCollapsed={true}
        streamingBlockId={null}
        onAnswer={noop}
      />,
    )
    expect(document.querySelector('strong')).not.toBeNull()
  })
})

describe('Turn question rendering', () => {
  const baseQ = {
    kind: 'question' as const,
    id: 'q1',
    questionId: 'q1',
    header: 'Layout',
    question: 'Two-pane?',
    options: [{ label: 'Yes' }],
    multiSelect: false,
  }

  it('does not render a LIVE question inline (it is pinned elsewhere)', () => {
    const { container } = render(
      <Turn
        turn={makeTurn([{ ...baseQ, live: true }])}
        isLastTurn
        toolDetailMode="auto"
        thinkingCollapsed={true}
        onAnswer={noop}
      />,
    )
    expect(screen.queryByText('Two-pane?')).toBeNull()
    // No stray empty wrapper div either
    expect(container).toBeEmptyDOMElement()
  })

  it('renders a historical (non-live) question inline, read-only', () => {
    render(
      <Turn
        turn={makeTurn([{ ...baseQ, live: false }])}
        isLastTurn
        toolDetailMode="auto"
        thinkingCollapsed={true}
        onAnswer={vi.fn()}
      />,
    )
    expect(screen.getByText('Two-pane?')).toBeInTheDocument()
    // Read-only: no interactive radio inputs
    expect(screen.queryByRole('radio')).toBeNull()
  })
})
