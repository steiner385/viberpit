import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ErrorCard } from '../ErrorCard'

describe('ErrorCard', () => {
  it('renders the error message inline as an alert', () => {
    render(<ErrorCard code="max_turns" message="Reached the maximum number of turns" />)
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('Reached the maximum number of turns')
  })

  it('shows the error code so the failure is identifiable', () => {
    render(<ErrorCard code="api_429" message="rate limited" />)
    expect(screen.getByText(/api_429/)).toBeInTheDocument()
  })

  it('shows a reset time when provided', () => {
    render(<ErrorCard code="rate_limit" message="slow down" resetsAt="2024-01-01T00:00:00Z" />)
    expect(screen.getByText(/Resets at/i)).toBeInTheDocument()
  })
})
