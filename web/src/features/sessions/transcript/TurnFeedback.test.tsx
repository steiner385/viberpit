import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

const mutate = vi.fn()
vi.mock('@/api/assistant', () => ({
  useAssistantFeedback: vi.fn(() => ({ mutate })),
}))
vi.mock('@/features/sessions/SessionRuntimeProvider', () => ({
  useSessionRuntimeContext: vi.fn(() => ({ sessionId: 'sess-1' })),
}))

import { TurnFeedback } from './TurnFeedback'

describe('TurnFeedback', () => {
  beforeEach(() => { mutate.mockClear() })

  it('renders Helpful and Not helpful buttons, unpressed', () => {
    render(<TurnFeedback turnId="turn-1" />)
    const up = screen.getByRole('button', { name: 'Helpful' })
    const down = screen.getByRole('button', { name: 'Not helpful' })
    expect(up).toHaveAttribute('aria-pressed', 'false')
    expect(down).toHaveAttribute('aria-pressed', 'false')
  })

  it('votes up: calls the mutation with sessionId/turnId/vote and shows pressed state', () => {
    render(<TurnFeedback turnId="turn-1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Helpful' }))
    expect(mutate).toHaveBeenCalledWith({ sessionId: 'sess-1', turnId: 'turn-1', vote: 'up' })
    expect(screen.getByRole('button', { name: 'Helpful' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Not helpful' })).toHaveAttribute('aria-pressed', 'false')
  })

  it('clicking the other button switches the vote', () => {
    render(<TurnFeedback turnId="turn-1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Helpful' }))
    fireEvent.click(screen.getByRole('button', { name: 'Not helpful' }))
    expect(mutate).toHaveBeenCalledTimes(2)
    expect(mutate).toHaveBeenLastCalledWith({ sessionId: 'sess-1', turnId: 'turn-1', vote: 'down' })
    expect(screen.getByRole('button', { name: 'Not helpful' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Helpful' })).toHaveAttribute('aria-pressed', 'false')
  })

  it('clicking an already-pressed vote is a no-op, not a second call', () => {
    render(<TurnFeedback turnId="turn-1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Helpful' }))
    fireEvent.click(screen.getByRole('button', { name: 'Helpful' }))
    expect(mutate).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: 'Helpful' })).toHaveAttribute('aria-pressed', 'true')
  })
})
