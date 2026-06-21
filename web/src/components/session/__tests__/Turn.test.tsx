import { describe, it, expect } from 'vitest'
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
  it('folds a long completed tool run when this is NOT the last turn', () => {
    render(
      <Turn
        turn={makeTurn([tool('a'), tool('b'), tool('c'), tool('d')])}
        permissionMode="interactive"
        isLastTurn={false}
        toolDetailMode="auto"
        onDecision={noop}
        onAnswer={noop}
      />,
    )
    expect(screen.getByRole('button', { name: /tool group/i }).textContent).toMatch(/4 tool calls/)
  })

  it('keeps the tool run expanded when this IS the last turn (live tail)', () => {
    render(
      <Turn
        turn={makeTurn([tool('a'), tool('b'), tool('c'), tool('d')])}
        permissionMode="interactive"
        isLastTurn
        toolDetailMode="auto"
        onDecision={noop}
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
        permissionMode="interactive"
        isLastTurn={false}
        toolDetailMode="auto"
        onDecision={noop}
        onAnswer={noop}
      />,
    )
    expect(screen.getByText('hello world')).toBeInTheDocument()
  })
})
