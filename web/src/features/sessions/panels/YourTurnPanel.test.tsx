import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { YourTurnPanel } from './YourTurnPanel'

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
    expect(onPrompt).toHaveBeenCalledWith('Proceed with Phase B', [], undefined, 'suggested')
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

// A hand-back is the one needs-you state with no card to answer: the session said
// what it wants in one sentence via hand_back(reason) and stopped. The list card
// already shows that sentence (#2321); inside the session the ask was prose at the
// bottom of the scroller with nothing pinned — the 2026-09-29 critique's P0.
describe('YourTurnPanel — handed back', () => {
  const REASON = 'Need a decision on whether to override the read-only constraint'

  it('pins a needs-you region that leads with the session\'s own reason', () => {
    render(
      <YourTurnPanel statusState="handed_back" handBackReason={REASON} suggestions={[]} onPrompt={vi.fn()} />,
    )
    const region = screen.getByRole('region', { name: /handed back/i })
    expect(region).toBeInTheDocument()
    expect(region).toHaveTextContent(REASON)
    // The calm "Your turn" wording must not leak into the needs-you state.
    expect(screen.queryByText(/reply to keep this session moving/i)).toBeNull()
  })

  it('says plainly that the session stopped when it handed back without a reason', () => {
    render(<YourTurnPanel statusState="handed_back" suggestions={[]} onPrompt={vi.fn()} />)
    expect(screen.getByRole('region', { name: /handed back/i })).toHaveTextContent(
      /stopped and is waiting on you/i,
    )
  })

  it('offers the cached suggestions as one-tap replies, like a finished turn', () => {
    const onPrompt = vi.fn()
    render(
      <YourTurnPanel
        statusState="handed_back"
        handBackReason={REASON}
        suggestions={['Override it', 'Keep it read-only']}
        onPrompt={onPrompt}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Keep it read-only' }))
    expect(onPrompt).toHaveBeenCalledWith('Keep it read-only', [], undefined, 'suggested')
  })

  it('surfaces the suspected-complete hint with the same two answers the card offers', () => {
    const onResolve = vi.fn()
    render(
      <YourTurnPanel
        statusState="handed_back"
        handBackReason="PR #2521 is open with auto-merge armed"
        suspectedCompleteReason="PR #2521 open with auto-merge armed"
        onResolveSuspectedComplete={onResolve}
        suggestions={[]}
        onPrompt={vi.fn()}
      />,
    )
    expect(screen.getByText(/likely complete/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /mark complete/i }))
    expect(onResolve).toHaveBeenCalledWith('confirm')
    fireEvent.click(screen.getByRole('button', { name: /not done/i }))
    expect(onResolve).toHaveBeenCalledWith('reject')
  })

  it('shows no completion buttons when nothing suspects the objective is met', () => {
    render(
      <YourTurnPanel
        statusState="handed_back"
        handBackReason={REASON}
        onResolveSuspectedComplete={vi.fn()}
        suggestions={[]}
        onPrompt={vi.fn()}
      />,
    )
    expect(screen.queryByRole('button', { name: /mark complete/i })).toBeNull()
    expect(screen.queryByText(/likely complete/i)).toBeNull()
  })

  it('disables both answers while the decision is in flight', () => {
    render(
      <YourTurnPanel
        statusState="handed_back"
        suspectedCompleteReason="PR #9 merged"
        onResolveSuspectedComplete={vi.fn()}
        resolvePending
        suggestions={[]}
        onPrompt={vi.fn()}
      />,
    )
    expect(screen.getByRole('button', { name: /mark complete/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: /not done/i })).toBeDisabled()
  })

  it('ignores a hand-back reason outside the handed_back status (a resumed session keeps the stale row value)', () => {
    render(
      <YourTurnPanel statusState="done" handBackReason={REASON} suggestions={[]} onPrompt={vi.fn()} />,
    )
    expect(screen.getByRole('region', { name: /your turn/i })).not.toHaveTextContent(REASON)
  })

  // #2647: a hand_back made mid-turn. The server writes the reason the moment the call
  // is read, so beside a busy status it is a live ask; the turn is still running.
  describe('a mid-turn ask (busy status with a hand-back reason)', () => {
    it.each(['in_progress', 'condensing', 'retrying'])('pins the ask while %s', (state) => {
      render(
        <YourTurnPanel statusState={state} handBackReason={REASON} suggestions={['Approved']} onPrompt={vi.fn()} />,
      )
      const region = screen.getByRole('region', { name: /asked you/i })
      expect(region).toHaveAttribute('data-mode', 'asked_mid_turn')
      expect(region).toHaveTextContent(REASON)
      expect(region).toHaveTextContent(/still working/i)
      expect(screen.getByRole('button', { name: 'Approved' })).toBeInTheDocument()
    })

    it('renders nothing while busy without a reason, and never for a parked session', () => {
      const a = render(<YourTurnPanel statusState="in_progress" suggestions={['x']} onPrompt={vi.fn()} />)
      expect(a.container).toBeEmptyDOMElement()
      a.unmount()
      const b = render(
        <YourTurnPanel statusState="waiting_external" handBackReason={REASON} suggestions={[]} onPrompt={vi.fn()} />,
      )
      expect(b.container).toBeEmptyDOMElement()
    })

    it('never shows the "Likely complete" hint mid-turn — it judges a finished turn', () => {
      render(
        <YourTurnPanel
          statusState="in_progress"
          handBackReason={REASON}
          suspectedCompleteReason="PR #1 open with auto-merge armed"
          onResolveSuspectedComplete={vi.fn()}
          suggestions={[]}
          onPrompt={vi.fn()}
        />,
      )
      expect(screen.queryByText(/likely complete/i)).toBeNull()
    })
  })
})
