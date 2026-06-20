import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { EmptyState } from '../EmptyState'

describe('EmptyState', () => {
  it('renders a "no sessions" message', () => {
    render(<EmptyState onNew={vi.fn()} />)
    expect(screen.getByText(/no sessions/i)).toBeInTheDocument()
  })

  it('renders a CTA button', () => {
    render(<EmptyState onNew={vi.fn()} />)
    const button = screen.getByRole('button', { name: /new session/i })
    expect(button).toBeInTheDocument()
  })

  it('calls onNew when the CTA button is clicked', async () => {
    const onNew = vi.fn()
    const user = userEvent.setup()
    render(<EmptyState onNew={onNew} />)
    await user.click(screen.getByRole('button', { name: /new session/i }))
    expect(onNew).toHaveBeenCalledOnce()
  })
})
