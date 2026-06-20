import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AllLimitedBanner } from '../AllLimitedBanner'

describe('AllLimitedBanner', () => {
  const ISO_RESET = '2026-06-19T14:30:00.000Z'

  it('shows the "all accounts at limit" message', () => {
    render(<AllLimitedBanner earliestReset={ISO_RESET} onDismiss={vi.fn()} />)
    expect(screen.getByText(/all accounts at limit/i)).toBeInTheDocument()
  })

  it('shows a formatted reset time including the month and day', () => {
    render(<AllLimitedBanner earliestReset={ISO_RESET} onDismiss={vi.fn()} />)
    // Intl.DateTimeFormat always includes month and day, regardless of locale
    // ISO_RESET = 2026-06-19T14:30:00.000Z → renders "Jun" and "19" in any ICU locale
    expect(screen.getByText(/jun/i)).toBeInTheDocument()
  })

  it('wraps the formatted time in a <time> element with dateTime attribute', () => {
    render(<AllLimitedBanner earliestReset={ISO_RESET} onDismiss={vi.fn()} />)
    const timeEl = document.querySelector('time')
    expect(timeEl).toBeInTheDocument()
    expect(timeEl?.getAttribute('dateTime')).toBe(ISO_RESET)
  })

  it('shows "No reset time available" when earliestReset is null', () => {
    render(<AllLimitedBanner earliestReset={null} onDismiss={vi.fn()} />)
    expect(screen.getByText(/no reset time available/i)).toBeInTheDocument()
  })

  it('calls onDismiss when the dismiss button is clicked', async () => {
    const onDismiss = vi.fn()
    const user = userEvent.setup()
    render(<AllLimitedBanner earliestReset={ISO_RESET} onDismiss={onDismiss} />)
    await user.click(screen.getByRole('button', { name: /dismiss/i }))
    expect(onDismiss).toHaveBeenCalledOnce()
  })
})
