/**
 * The visible half of prompt-receipt feedback: a sent-but-unconfirmed prompt renders
 * a stage note under its bubble, so the wait has a name instead of being a blank
 * transcript.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { PendingPromptNote } from './PendingPromptNote'

describe('PendingPromptNote', () => {
  it('names the server-side step for a session being woken', () => {
    render(<PendingPromptNote stage="resuming" />)
    expect(screen.getByText(/waking session/i)).toBeInTheDocument()
  })

  it('says a prompt is held, not sent, when the socket is down', () => {
    render(<PendingPromptNote stage="buffered" />)
    expect(screen.getByText(/waiting to reconnect/i)).toBeInTheDocument()
  })

  it('confirms receipt once the edge has acked', () => {
    render(<PendingPromptNote stage="received" />)
    expect(screen.getByText(/received/i)).toBeInTheDocument()
  })

  it('shows a spinner while the prompt is still in flight', () => {
    render(<PendingPromptNote stage="sent" />)
    expect(screen.getByTestId('pending-prompt-spinner')).toBeInTheDocument()
  })

  it('drops the spinner and warns once the prompt has stalled', () => {
    render(<PendingPromptNote stage="stalled" />)
    expect(screen.queryByTestId('pending-prompt-spinner')).not.toBeInTheDocument()
    expect(screen.getByText(/no response from the server/i)).toBeInTheDocument()
  })

  it('announces the stage to assistive tech politely', () => {
    render(<PendingPromptNote stage="resuming" />)
    expect(screen.getByRole('status')).toHaveTextContent(/waking session/i)
  })
})
