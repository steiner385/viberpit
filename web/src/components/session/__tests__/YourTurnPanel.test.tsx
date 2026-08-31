import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { YourTurnPanel } from '../YourTurnPanel'

describe('YourTurnPanel', () => {
  it.each(['in_progress', 'waiting_external', 'waiting_question', 'idle', 'complete', 'ended'])(
    'renders nothing for status %s',
    (state) => {
      const { container } = render(
        <YourTurnPanel statusState={state} suggestions={['Run the tests']} onPrompt={vi.fn()} />,
      )
      expect(container).toBeEmptyDOMElement()
    },
  )

  it('shows a prominent call to action when the turn is done (Your turn)', () => {
    render(<YourTurnPanel statusState="done" suggestions={[]} onPrompt={vi.fn()} />)
    const region = screen.getByRole('region', { name: /your turn/i })
    expect(region).toBeInTheDocument()
    expect(screen.getByText('Your turn')).toBeInTheDocument()
    expect(screen.getByText(/reply to keep this session moving/i)).toBeInTheDocument()
  })

  it('renders each cached suggestion as a one-tap reply that sends immediately', () => {
    const onPrompt = vi.fn()
    render(
      <YourTurnPanel
        statusState="done"
        suggestions={['Run the tests', 'Proceed with Phase B']}
        onPrompt={onPrompt}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Proceed with Phase B' }))
    expect(onPrompt).toHaveBeenCalledWith('Proceed with Phase B', [])
  })

  it('falls back to a review-and-reply hint when no suggestions are cached', () => {
    render(<YourTurnPanel statusState="done" suggestions={[]} onPrompt={vi.fn()} />)
    expect(screen.getByText(/review the last message above, then reply below/i)).toBeInTheDocument()
  })

  it('does not show the fallback hint when suggestions are present', () => {
    render(<YourTurnPanel statusState="done" suggestions={['Continue']} onPrompt={vi.fn()} />)
    expect(screen.queryByText(/review the last message above/i)).toBeNull()
  })
})
