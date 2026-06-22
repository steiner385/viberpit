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

  it('renders both escape hatches for single-select', () => {
    render(<QuestionCard block={singleQ} onAnswer={vi.fn()} onChat={vi.fn()} />)
    expect(screen.getByRole('button', { name: /write a response/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /chat about this/i })).toBeInTheDocument()
  })

  it('renders both escape hatches for multi-select', () => {
    render(<QuestionCard block={multiQ} onAnswer={vi.fn()} onChat={vi.fn()} />)
    expect(screen.getByRole('button', { name: /write a response/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /chat about this/i })).toBeInTheDocument()
  })

  it('free-form: typing and sending calls onAnswer with mode "freeform"', async () => {
    const user = userEvent.setup()
    const onAnswer = vi.fn()
    render(<QuestionCard block={singleQ} onAnswer={onAnswer} onChat={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: /write a response/i }))
    await user.type(screen.getByRole('textbox', { name: /write a response/i }), 'my own answer')
    await user.click(screen.getByRole('button', { name: /send response/i }))
    expect(onAnswer).toHaveBeenCalledWith('q-1', ['my own answer'], 'freeform')
  })

  it('free-form: empty input does not submit', async () => {
    const user = userEvent.setup()
    const onAnswer = vi.fn()
    render(<QuestionCard block={singleQ} onAnswer={onAnswer} onChat={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: /write a response/i }))
    await user.click(screen.getByRole('button', { name: /send response/i }))
    expect(onAnswer).not.toHaveBeenCalled()
  })

  it('chat: clicking "Chat about this" calls onChat', async () => {
    const user = userEvent.setup()
    const onChat = vi.fn()
    render(<QuestionCard block={singleQ} onAnswer={vi.fn()} onChat={onChat} />)
    await user.click(screen.getByRole('button', { name: /chat about this/i }))
    expect(onChat).toHaveBeenCalledWith('q-1')
  })

  it('armedForChat: options collapse to a banner and Cancel calls onChat', async () => {
    const user = userEvent.setup()
    const onChat = vi.fn()
    render(<QuestionCard block={singleQ} onAnswer={vi.fn()} onChat={onChat} armedForChat />)
    expect(screen.queryByRole('radio', { name: 'Option A' })).not.toBeInTheDocument()
    expect(screen.getByText(/replying in chat/i)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /cancel/i }))
    expect(onChat).toHaveBeenCalledWith('q-1')
  })

  it('single-select: digit 2 answers with the second option', async () => {
    const user = userEvent.setup()
    const onAnswer = vi.fn()
    render(<QuestionCard block={singleQ} onAnswer={onAnswer} />)
    await user.keyboard('2')
    expect(onAnswer).toHaveBeenCalledWith('q-1', ['Option B'])
  })

  it('single-select: ArrowDown then Enter answers the highlighted option', async () => {
    const user = userEvent.setup()
    const onAnswer = vi.fn()
    render(<QuestionCard block={singleQ} onAnswer={onAnswer} />)
    await user.keyboard('{ArrowDown}{Enter}')
    expect(onAnswer).toHaveBeenCalledWith('q-1', ['Option B'])
  })

  it('single-select: first option is focused on mount', () => {
    render(<QuestionCard block={singleQ} onAnswer={vi.fn()} />)
    expect(screen.getByRole('radio', { name: 'Option A' })).toHaveFocus()
  })

  it('multi-select: digit toggles aria-checked', async () => {
    const user = userEvent.setup()
    render(<QuestionCard block={multiQ} onAnswer={vi.fn()} />)
    const optA = screen.getByRole('checkbox', { name: /option a/i })
    expect(optA).toHaveAttribute('aria-checked', 'false')
    await user.keyboard('1')
    expect(optA).toHaveAttribute('aria-checked', 'true')
  })

  it('multi-select: digit-toggle then Enter submits the checked set', async () => {
    const user = userEvent.setup()
    const onAnswer = vi.fn()
    render(<QuestionCard block={multiQ} onAnswer={onAnswer} />)
    await user.keyboard('1')        // toggle Option A
    await user.keyboard('{Enter}')  // submit
    expect(onAnswer).toHaveBeenCalledWith('q-2', ['Option A'])
  })

  it('multi-select: Space toggles the highlighted option', async () => {
    const user = userEvent.setup()
    const onAnswer = vi.fn()
    render(<QuestionCard block={multiQ} onAnswer={onAnswer} />)
    await user.keyboard(' ')        // toggle highlighted (Option A)
    await user.keyboard('{Enter}')
    expect(onAnswer).toHaveBeenCalledWith('q-2', ['Option A'])
  })

  it('multi-select: typing a digit into the freeform textarea does not toggle', async () => {
    const user = userEvent.setup()
    render(<QuestionCard block={multiQ} onAnswer={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: /write a response/i }))
    const textarea = screen.getByRole('textbox', { name: /write a response/i })
    await user.type(textarea, '2')
    expect(textarea).toHaveValue('2')
    expect(screen.getByRole('checkbox', { name: /option b/i })).toHaveAttribute('aria-checked', 'false')
  })
})

describe('QuestionCard readOnly', () => {
  it('renders options inertly and never calls onAnswer when readOnly', async () => {
    const user = userEvent.setup()
    const onAnswer = vi.fn()
    render(<QuestionCard block={singleQ} onAnswer={onAnswer} readOnly />)
    expect(screen.getByText('Choose an option')).toBeInTheDocument()
    expect(screen.getByText('Which would you prefer?')).toBeInTheDocument()
    expect(screen.getByText('Option A')).toBeInTheDocument()
    // No interactive option buttons (read-only): clicking the text does nothing.
    await user.click(screen.getByText('Option A'))
    expect(onAnswer).not.toHaveBeenCalled()
    // No write/chat escape hatches in read-only mode.
    expect(screen.queryByText(/Write a response/)).toBeNull()
    expect(screen.queryByText(/Chat about this/)).toBeNull()
  })

  it('interactive mode still answers on option click', async () => {
    const user = userEvent.setup()
    const onAnswer = vi.fn()
    render(<QuestionCard block={singleQ} onAnswer={onAnswer} />)
    await user.click(screen.getByRole('radio', { name: 'Option A' }))
    expect(onAnswer).toHaveBeenCalledWith('q-1', ['Option A'])
  })
})
