import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QuestionCard } from '../session/QuestionCard'

const block = (over: Record<string, unknown> = {}) => ({
  kind: 'question' as const, id: 'toolu_A-0', questionId: 'toolu_A-0',
  header: 'Disposition', question: 'How should I dispose of #15205?',
  options: [{ label: 'Close it' }], multiSelect: false, live: true, ...over,
})

describe('QuestionCard orphaned notice', () => {
  it('explains that answering starts a new turn', () => {
    render(<QuestionCard block={block({ orphaned: true }) as never} onAnswer={vi.fn()} />)
    expect(screen.getByTestId('orphaned-question-notice')).toBeInTheDocument()
    expect(screen.getByTestId('orphaned-question-notice').textContent?.toLowerCase())
      .toContain('restart')
  })

  it('shows no notice for a normal live question', () => {
    render(<QuestionCard block={block() as never} onAnswer={vi.fn()} />)
    expect(screen.queryByTestId('orphaned-question-notice')).toBeNull()
  })

  it('keeps the options clickable so the answer can actually be given', async () => {
    const onAnswer = vi.fn()
    render(<QuestionCard block={block({ orphaned: true }) as never} onAnswer={onAnswer} />)
    screen.getByRole('radio', { name: /Close it/ }).click()
    expect(onAnswer).toHaveBeenCalled()
  })
})
