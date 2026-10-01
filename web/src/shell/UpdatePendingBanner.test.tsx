import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { UpdatePendingBanner } from './UpdatePendingBanner'

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
    expect(screen.queryByText(/message/i)).toBeNull()
  })

  it('the spinner yields to prefers-reduced-motion, like the daemon banner\'s', () => {
    const { container } = render(<UpdatePendingBanner queuedCount={0} />)
    const spinner = container.querySelector('.animate-spin')
    // `toHaveClass`, not `.className`: an SVG's className is an SVGAnimatedString.
    expect(spinner).toHaveClass('motion-reduce:animate-none')
  })

  it('shows a reconnecting message and held count when mode is reconnecting', () => {
    render(<UpdatePendingBanner queuedCount={1} mode="reconnecting" />)
    expect(screen.getByText(/reconnecting/i)).toBeInTheDocument()
    expect(screen.getByText(/1 message/i)).toBeInTheDocument()
  })
})
