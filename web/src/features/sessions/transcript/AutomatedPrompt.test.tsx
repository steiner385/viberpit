import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Turn } from './Turn'
import type { Turn as TurnType, Block } from '@/session/reducer'
import type { PromptOrigin } from '@/session/events'

function textBlock(text: string): Block {
  return { kind: 'text', id: 'b1', text }
}

function userTurn(text: string, origin?: PromptOrigin): TurnType {
  return { id: 'turn-1', role: 'user', blocks: [textBlock(text)], ...(origin ? { origin } : {}) }
}

const noop = () => {}

function renderTurn(turn: TurnType) {
  return render(
    <Turn turn={turn} isLastTurn={false} toolDetailMode="auto" thinkingCollapsed onAnswer={noop} />,
  )
}

describe('prompt attribution', () => {
  it('renders a typed prompt as the right-aligned "you" bubble, captioned', () => {
    renderTurn(userTurn('ship it'))
    expect(screen.getByText('ship it')).toBeInTheDocument()
    expect(screen.getByText('You')).toBeInTheDocument()
    // The whole point: a human prompt is NOT the automated card.
    expect(screen.queryByTestId('automated-prompt')).toBeNull()
  })

  it('renders an autopilot nudge as a captioned card, not a "you" bubble', () => {
    renderTurn(userTurn('Re-run the failing test.', { kind: 'autopilot' }))
    const card = screen.getByTestId('automated-prompt')
    expect(card).toHaveAttribute('data-origin-kind', 'autopilot')
    expect(screen.getByText('Autopilot')).toBeInTheDocument()
    expect(screen.getByText('Re-run the failing test.')).toBeInTheDocument()
    // The "you" caption must NOT appear — claiming the reader wrote this is the bug.
    expect(screen.queryByText('You')).toBeNull()
  })

  it('names the specific automation when the origin carries a label', () => {
    renderTurn(userTurn('rebase onto master', { kind: 'automation', label: 'PR Doctor' }))
    expect(screen.getByText('PR Doctor')).toBeInTheDocument()
    expect(screen.queryByText('Automation')).toBeNull()
  })

  it('falls back to the kind label when no label is supplied', () => {
    renderTurn(userTurn('goal not met', { kind: 'completion' }))
    expect(screen.getByText('Completion check')).toBeInTheDocument()
  })

  it('marks a backfilled origin as reconstructed', () => {
    renderTurn(userTurn('an old nudge', { kind: 'autopilot', inferred: true }))
    expect(screen.getByText('· reconstructed')).toBeInTheDocument()
  })

  it('does not mark a forward-tagged origin as reconstructed', () => {
    renderTurn(userTurn('a fresh nudge', { kind: 'autopilot' }))
    expect(screen.queryByText('· reconstructed')).toBeNull()
  })

  it('leaves assistant turns alone', () => {
    render(
      <Turn
        turn={{ id: 't', role: 'assistant', blocks: [textBlock('done')] }}
        isLastTurn={false} toolDetailMode="auto" thinkingCollapsed onAnswer={noop}
      />,
    )
    expect(screen.queryByText('You')).toBeNull()
    expect(screen.queryByTestId('automated-prompt')).toBeNull()
  })
})
