import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import userEvent from '@testing-library/user-event'
import { PendingQuestionPanel } from '../PendingQuestionPanel'

const q = (id: string, live: boolean) => ({
  kind: 'question' as const, id, questionId: id, header: id, question: `${id}?`,
  options: [{ label: 'Yes' }], multiSelect: false, live,
})
let _tid = 0
const turn = (...blocks: ReturnType<typeof q>[]) => ({ id: `t-${_tid++}`, role: 'assistant' as const, blocks })

describe('PendingQuestionPanel', () => {
  it('renders nothing when there is no live question (detached/replay)', () => {
    const { container } = render(
      <PendingQuestionPanel turns={[turn(q('a', false))]} onAnswer={vi.fn()} onChat={vi.fn()} armedQuestionId={null} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('pins the first LIVE open question and answers it synchronously on click', () => {
    const onAnswer = vi.fn()
    render(<PendingQuestionPanel turns={[turn(q('a', true))]} onAnswer={onAnswer} onChat={vi.fn()} armedQuestionId={null} />)
    expect(screen.getByText('a?')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('radio', { name: 'Yes' }))
    expect(onAnswer).toHaveBeenCalledWith('a', ['Yes'])   // immediate, synchronous send
  })

  it('shows all live questions of a turn together and batches submit', async () => {
    const user = userEvent.setup()
    const onAnswer = vi.fn()
    render(
      <PendingQuestionPanel turns={[turn(q('a', true), q('b', true))]} onAnswer={onAnswer} onChat={vi.fn()} armedQuestionId={null} />)
    // Both questions are visible at once.
    expect(screen.getByText('a?')).toBeInTheDocument()
    expect(screen.getByText('b?')).toBeInTheDocument()
    // Nothing is sent until the group submit fires.
    const submit = screen.getByRole('button', { name: /submit all answers/i })
    expect(submit).toBeDisabled()
    await user.click(screen.getAllByRole('radio', { name: 'Yes' })[0]) // a
    await user.click(screen.getAllByRole('radio', { name: 'Yes' })[1]) // b
    expect(onAnswer).not.toHaveBeenCalled()
    await user.click(submit)
    expect(onAnswer).toHaveBeenCalledTimes(2)
  })

  it('chat-arm: clicking "Chat about this" calls onChat with the question id', async () => {
    const user = userEvent.setup()
    const onChat = vi.fn()
    render(
      <PendingQuestionPanel turns={[turn(q('q1', true))]} onAnswer={vi.fn()} onChat={onChat} armedQuestionId={null} />
    )
    await user.click(screen.getByRole('button', { name: /chat about this/i }))
    expect(onChat).toHaveBeenCalledWith('q1')
  })

  it('exposes role="region" with accessible name "Pending question" when a live question is present', () => {
    render(
      <PendingQuestionPanel turns={[turn(q('r1', true))]} onAnswer={vi.fn()} onChat={vi.fn()} armedQuestionId={null} />
    )
    expect(screen.getByRole('region', { name: /pending question/i })).toBeInTheDocument()
  })

  it('freeform: clicking "Write a response", typing, and "Send response" calls onAnswer with freeform mode', async () => {
    const user = userEvent.setup()
    const onAnswer = vi.fn()
    render(
      <PendingQuestionPanel turns={[turn(q('q2', true))]} onAnswer={onAnswer} onChat={vi.fn()} armedQuestionId={null} />
    )
    await user.click(screen.getByRole('button', { name: /write a response/i }))
    await user.type(screen.getByRole('textbox', { name: /write a response/i }), 'my freeform answer')
    await user.click(screen.getByRole('button', { name: /send response/i }))
    expect(onAnswer).toHaveBeenCalledWith('q2', ['my freeform answer'], 'freeform')
  })

  it('does not carry a freeform draft from one question to the next', async () => {
    const user = userEvent.setup()
    const { rerender } = render(
      <PendingQuestionPanel turns={[turn(q('a', true))]} onAnswer={vi.fn()} onChat={vi.fn()} armedQuestionId={null} />)
    await user.click(screen.getByRole('button', { name: /write a response/i }))
    await user.type(screen.getByRole('textbox', { name: /write a response/i }), 'leaky draft')
    // Question 'a' is answered/removed; 'b' takes its place at the same tree slot.
    rerender(
      <PendingQuestionPanel turns={[turn(q('b', true))]} onAnswer={vi.fn()} onChat={vi.fn()} armedQuestionId={null} />)
    // The new question shows its options fresh — no textarea, no leaked text.
    expect(screen.queryByRole('textbox', { name: /write a response/i })).toBeNull()
    expect(screen.queryByText('leaky draft')).toBeNull()
  })
})
