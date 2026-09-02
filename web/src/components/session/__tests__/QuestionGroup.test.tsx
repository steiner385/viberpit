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

  it('lays the questions out in a horizontal snap carousel, not a vertical stack', () => {
    render(<QuestionGroup questions={[mk('a'), mk('b')]} onAnswer={vi.fn()} />)
    const track = screen.getByTestId('question-carousel')
    // horizontal scroll-snap track — the swipe-left/right behaviour
    expect(track.className).toMatch(/overflow-x-auto/)
    expect(track.className).toMatch(/snap-x/)
    // each question is a full-width, non-shrinking snap target
    for (const item of Array.from(track.children)) {
      expect(item.className).toMatch(/snap-(center|start)/)
      expect(item.className).toMatch(/w-full/)
      expect(item.className).toMatch(/shrink-0/)
    }
    // panel height follows the active card (animated), cards sized naturally
    expect(track.className).toMatch(/items-start/)
    expect(track.className).toMatch(/transition-\[height\]/)
  })

  it('shows a position indicator and steps through with prev/next', async () => {
    const user = userEvent.setup()
    render(<QuestionGroup questions={[mk('a'), mk('b'), mk('c')]} onAnswer={vi.fn()} />)
    expect(screen.getByText(/question 1 of 3/i)).toBeInTheDocument()
    const next = screen.getByRole('button', { name: /next question/i })
    const prev = screen.getByRole('button', { name: /previous question/i })
    expect(prev).toBeDisabled()
    await user.click(next)
    expect(screen.getByText(/question 2 of 3/i)).toBeInTheDocument()
    expect(prev).toBeEnabled()
  })

  it('starts the carousel on the first question, not the last', () => {
    render(<QuestionGroup questions={[mk('a'), mk('b'), mk('c')]} onAnswer={vi.fn()} />)
    // indicator points at the first question…
    expect(screen.getByText(/question 1 of 3/i)).toBeInTheDocument()
    // …and the snap track is scrolled to the start, not snapped to the last card
    expect(screen.getByTestId('question-carousel').scrollLeft).toBe(0)
    // Regression guard (jsdom has no scroll-snap engine, so runtime scrollLeft
    // can't catch this): the track must use `proximity`, not `mandatory`.
    // `mandatory` forces the container to always rest on a snap point and
    // re-resolves the target on every relayout (trackH application, card
    // expansion), which the browser can resolve to the *last* card — the exact
    // jump this test's name forbids.
    const track = screen.getByTestId('question-carousel')
    expect(track.className).toMatch(/snap-proximity/)
    expect(track.className).not.toMatch(/snap-mandatory/)
  })

  it('does not let a later card steal focus on mount (which jumps the indicator to the last question)', () => {
    render(<QuestionGroup questions={[mk('a'), mk('b'), mk('c')]} onAnswer={vi.fn()} />)
    // Each card auto-focuses its first option on mount. In a shared horizontal
    // scroll track, a *later* card winning that focus scrolls the track to the
    // end, firing onScroll → setActive(lastIndex) — the "Question 3 of 3 on
    // open" bug. Only the active (first) card may hold focus after mount.
    const firstOptions = screen.getAllByRole('radio', { name: 'Yes' })
    expect(document.activeElement).toBe(firstOptions[0])
    // …and the indicator stays on the first question.
    expect(screen.getByText(/question 1 of 3/i)).toBeInTheDocument()
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
