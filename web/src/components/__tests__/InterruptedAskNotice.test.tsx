import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { InterruptedAskNotice } from '../session/InterruptedAskNotice'

describe('InterruptedAskNotice', () => {
  it('explains a lost permission prompt', () => {
    render(<InterruptedAskNotice interruptedFromStatus="waiting_input" onAskAgain={vi.fn()} />)
    const el = screen.getByTestId('interrupted-ask-notice')
    expect(el.textContent?.toLowerCase()).toContain('permission')
    expect(el.textContent?.toLowerCase()).toContain('restart')
  })

  it('explains a lost plan approval', () => {
    render(<InterruptedAskNotice interruptedFromStatus="ready_review" onAskAgain={vi.fn()} />)
    expect(screen.getByTestId('interrupted-ask-notice').textContent?.toLowerCase())
      .toContain('plan')
  })

  it('scopes the live region to the copy, not the button', () => {
    render(<InterruptedAskNotice interruptedFromStatus="waiting_input" onAskAgain={vi.fn()} />)
    // role="status" on the outer wrapper would make the announcement include the
    // "Ask again" button label; it belongs on the span carrying the message.
    const status = screen.getByRole('status')
    expect(status.textContent?.toLowerCase()).toContain('permission')
    expect(status.textContent?.toLowerCase()).not.toContain('ask again')
    expect(screen.getByTestId('interrupted-ask-notice')).not.toHaveAttribute('role')
  })

  it('asks again on click', () => {
    const onAskAgain = vi.fn()
    render(<InterruptedAskNotice interruptedFromStatus="waiting_input" onAskAgain={onAskAgain} />)
    screen.getByRole('button', { name: /ask again/i }).click()
    expect(onAskAgain).toHaveBeenCalledTimes(1)
  })

  it('renders nothing for a question (QuestionCard owns that case)', () => {
    const { container } = render(
      <InterruptedAskNotice interruptedFromStatus="waiting_question" onAskAgain={vi.fn()} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing for a healthy session', () => {
    const { container } = render(
      <InterruptedAskNotice interruptedFromStatus={null} onAskAgain={vi.fn()} />)
    expect(container).toBeEmptyDOMElement()
  })
})
