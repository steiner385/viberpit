import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QuestionGroup } from '../QuestionGroup'
import type { Block } from '@/session/reducer'

type QuestionBlock = Extract<Block, { kind: 'question' }>

const mk = (id: string, multiSelect = false): QuestionBlock => ({
  kind: 'question', id, questionId: id, header: `H-${id}`, question: `${id}?`,
  options: [{ label: 'Yes' }, { label: 'No' }], multiSelect, live: true,
})

describe('QuestionGroup', () => {
  it('renders every question in the group', () => {
    render(<QuestionGroup questions={[mk('a'), mk('b')]} onAnswer={vi.fn()} />)
    expect(screen.getByText('a?')).toBeInTheDocument()
    expect(screen.getByText('b?')).toBeInTheDocument()
  })

  it('disables submit until all questions are answered', async () => {
    const user = userEvent.setup()
    render(<QuestionGroup questions={[mk('a'), mk('b')]} onAnswer={vi.fn()} />)
    const submit = screen.getByRole('button', { name: /submit all answers/i })
    expect(submit).toBeDisabled()
    await user.click(screen.getAllByRole('radio', { name: 'Yes' })[0])   // answer 'a'
    expect(submit).toBeDisabled()                                   // 'b' still open
    await user.click(screen.getAllByRole('radio', { name: 'No' })[1]) // answer 'b'
    expect(submit).toBeEnabled()
  })

  it('sends one onAnswer per question on submit, in order', async () => {
    const user = userEvent.setup()
    const onAnswer = vi.fn()
    render(<QuestionGroup questions={[mk('a'), mk('b')]} onAnswer={onAnswer} />)
    await user.click(screen.getAllByRole('radio', { name: 'Yes' })[0]) // a → Yes
    await user.click(screen.getAllByRole('radio', { name: 'No' })[1])  // b → No
    await user.click(screen.getByRole('button', { name: /submit all answers/i }))
    expect(onAnswer).toHaveBeenCalledTimes(2)
    expect(onAnswer).toHaveBeenNthCalledWith(1, 'a', ['Yes'], 'option')
    expect(onAnswer).toHaveBeenNthCalledWith(2, 'b', ['No'], 'option')
  })

  it('lets the user change an answer before submitting', async () => {
    const user = userEvent.setup()
    const onAnswer = vi.fn()
    render(<QuestionGroup questions={[mk('a')]} onAnswer={onAnswer} />)
    await user.click(screen.getByRole('radio', { name: 'Yes' }))
    await user.click(screen.getByRole('radio', { name: 'No' }))  // change before submit
    await user.click(screen.getByRole('button', { name: /submit all answers/i }))
    expect(onAnswer).toHaveBeenCalledTimes(1)
    expect(onAnswer).toHaveBeenCalledWith('a', ['No'], 'option')
  })

  it('treats an empty multi-select as unanswered', async () => {
    const user = userEvent.setup()
    render(<QuestionGroup questions={[mk('m', true)]} onAnswer={vi.fn()} />)
    const submit = screen.getByRole('button', { name: /submit all answers/i })
    expect(submit).toBeDisabled()
    await user.click(screen.getByRole('checkbox', { name: /yes/i }))   // now 1 selected
    expect(submit).toBeEnabled()
    await user.click(screen.getByRole('checkbox', { name: /yes/i }))   // back to empty
    expect(submit).toBeDisabled()
  })
})
