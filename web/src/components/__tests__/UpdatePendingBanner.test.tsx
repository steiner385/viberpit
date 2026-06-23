import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { UpdatePendingBanner } from '../UpdatePendingBanner'

describe('UpdatePendingBanner', () => {
  it('explains the update is applying', () => {
    render(<UpdatePendingBanner queuedCount={0} />)
    expect(screen.getByText(/update in progress/i)).toBeInTheDocument()
  })

  it('reports how many messages are queued', () => {
    render(<UpdatePendingBanner queuedCount={2} />)
    expect(screen.getByText(/2 message/i)).toBeInTheDocument()
  })

  it('omits the queue line when nothing is queued', () => {
    render(<UpdatePendingBanner queuedCount={0} />)
    expect(screen.queryByText(/will send after/i)).toBeNull()
  })
})
