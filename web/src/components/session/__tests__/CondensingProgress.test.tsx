import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import { CondensingProgress } from '../CondensingProgress'

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

describe('CondensingProgress', () => {
  it('shows the label and a progressbar that stays under 100% before the end', () => {
    render(<CondensingProgress expectedMs={10000} />)
    expect(screen.getByText(/Condensing context…/)).toBeInTheDocument()
    const bar = screen.getByRole('progressbar')
    act(() => { vi.advanceTimersByTime(5000) })   // halfway → ~47% (linear to 95%)
    const mid = Number(bar.getAttribute('aria-valuenow'))
    expect(mid).toBeGreaterThan(0)
    expect(mid).toBeLessThan(95)
    act(() => { vi.advanceTimersByTime(60000) })  // long overrun → asymptote, still < 100
    expect(Number(bar.getAttribute('aria-valuenow'))).toBeLessThan(100)
  })
})
