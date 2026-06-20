import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QuestionCard } from '../QuestionCard'
import type { Block } from '@/session/reducer'

type QuestionBlock = Extract<Block, { kind: 'question' }>

const singleQ: QuestionBlock = {
  kind: 'question',
  id: 'q-1',
  questionId: 'q-1',
  header: 'Choose an option',
  question: 'Which would you prefer?',
  options: [{ label: 'Option A' }, { label: 'Option B' }],
  multiSelect: false,
}

const multiQ: QuestionBlock = {
  ...singleQ,
  id: 'q-2',
  questionId: 'q-2',
  header: 'Choose multiple',
  question: 'Pick all that apply',
  multiSelect: true,
}

describe('QuestionCard', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('renders header and question', () => {
    render(<QuestionCard block={singleQ} onAnswer={vi.fn()} />)
    expect(screen.getByText('Choose an option')).toBeInTheDocument()
    expect(screen.getByText('Which would you prefer?')).toBeInTheDocument()
  })

  it('renders options', () => {
    render(<QuestionCard block={singleQ} onAnswer={vi.fn()} />)
    expect(screen.getByText('Option A')).toBeInTheDocument()
    expect(screen.getByText('Option B')).toBeInTheDocument()
  })

  it('single-select auto-submits when an option is clicked', async () => {
    const user = userEvent.setup()
    const onAnswer = vi.fn()
    render(<QuestionCard block={singleQ} onAnswer={onAnswer} />)
    await user.click(screen.getByRole('radio', { name: 'Option A' }))
    expect(onAnswer).toHaveBeenCalledWith('q-1', ['Option A'])
  })

  it('single-select uses radiogroup role', () => {
    render(<QuestionCard block={singleQ} onAnswer={vi.fn()} />)
    expect(screen.getByRole('radiogroup')).toBeInTheDocument()
  })

  it('multi-select renders checkboxes', () => {
    render(<QuestionCard block={multiQ} onAnswer={vi.fn()} />)
    const checkboxes = screen.getAllByRole('checkbox')
    expect(checkboxes).toHaveLength(2)
  })

  it('multi-select submits selected items on Submit click', async () => {
    const user = userEvent.setup()
    const onAnswer = vi.fn()
    render(<QuestionCard block={multiQ} onAnswer={onAnswer} />)
    await user.click(screen.getByRole('checkbox', { name: /option a/i }))
    await user.click(screen.getByRole('button', { name: /submit/i }))
    expect(onAnswer).toHaveBeenCalledWith('q-2', ['Option A'])
  })

  it('multi-select collects multiple selections', async () => {
    const user = userEvent.setup()
    const onAnswer = vi.fn()
    render(<QuestionCard block={multiQ} onAnswer={onAnswer} />)
    await user.click(screen.getByRole('checkbox', { name: /option a/i }))
    await user.click(screen.getByRole('checkbox', { name: /option b/i }))
    await user.click(screen.getByRole('button', { name: /submit/i }))
    expect(onAnswer).toHaveBeenCalledWith('q-2', expect.arrayContaining(['Option A', 'Option B']))
  })
})
