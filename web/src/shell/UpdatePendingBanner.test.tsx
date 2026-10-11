import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
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

  it('says a session refusal is a retry, not a lost connection', () => {
    render(<UpdatePendingBanner queuedCount={2} mode="retrying" />)
    expect(screen.getByText(/not delivered yet/i)).toBeInTheDocument()
    expect(screen.queryByText(/connection lost/i)).toBeNull()
  })

  // The escape hatch: a hold that never clears on its own (2026-10-10) must not leave
  // the user staring at a spinner with their messages trapped.
  it('offers Send now and Discard while messages are held', () => {
    const onSendNow = vi.fn()
    const onDiscard = vi.fn()
    render(<UpdatePendingBanner queuedCount={2} onSendNow={onSendNow} onDiscard={onDiscard} />)
    fireEvent.click(screen.getByRole('button', { name: /send now/i }))
    fireEvent.click(screen.getByRole('button', { name: /discard/i }))
    expect(onSendNow).toHaveBeenCalledOnce()
    expect(onDiscard).toHaveBeenCalledOnce()
  })

  it('offers no Discard when nothing is held', () => {
    render(<UpdatePendingBanner queuedCount={0} onSendNow={vi.fn()} onDiscard={vi.fn()} />)
    expect(screen.getByRole('button', { name: /send now/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /discard/i })).toBeNull()
  })
})
