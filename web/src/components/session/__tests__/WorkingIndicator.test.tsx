import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import { WorkingIndicator } from '../WorkingIndicator'
import { WORKING_WORDS } from '../workingWords'

/**
 * Helper: install a matchMedia stub on window.
 * @param matches  true → prefers-reduced-motion: reduce
 */
function stubMatchMedia(matches: boolean) {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    configurable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: query === '(prefers-reduced-motion: reduce)' ? matches : false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  })
}

describe('WorkingIndicator', () => {
  beforeEach(() => {
    // Default: motion allowed
    stubMatchMedia(false)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders a word from the WORKING_WORDS list', () => {
    render(<WorkingIndicator />)
    // The animated word is aria-hidden; query by text across the whole document.
    const found = WORKING_WORDS.some((w) => document.body.textContent?.includes(w))
    expect(found).toBe(true)
  })

  it('has a sr-only "Working…" element when no count', () => {
    render(<WorkingIndicator />)
    const srEl = document.querySelector('.sr-only')
    expect(srEl).not.toBeNull()
    expect(srEl?.textContent).toBe('Working…')
  })

  it('shows "Working, N queued" in the sr-only label when count > 0', () => {
    render(<WorkingIndicator count={3} />)
    const srEl = document.querySelector('.sr-only')
    expect(srEl?.textContent).toBe('Working, 3 queued')
  })

  it('shows the queued count in visible text when count > 0', () => {
    render(<WorkingIndicator count={2} />)
    // The aria-hidden visual span renders "· 2 queued"
    expect(screen.getByText(/·\s*2 queued/)).toBeInTheDocument()
  })

  describe('with reduced-motion', () => {
    beforeEach(() => {
      stubMatchMedia(true)
    })

    it('does not crash with reduced-motion enabled', () => {
      expect(() => render(<WorkingIndicator />)).not.toThrow()
    })

    it('still shows a word from the list', () => {
      render(<WorkingIndicator />)
      const found = WORKING_WORDS.some((w) => document.body.textContent?.includes(w))
      expect(found).toBe(true)
    })

    it('does not cycle the spinner glyph when reduced-motion is set', () => {
      vi.useFakeTimers()
      render(<WorkingIndicator />)
      const before = document.querySelector('[data-testid="working-spinner"]')?.textContent
      act(() => { vi.advanceTimersByTime(2000) })
      const after = document.querySelector('[data-testid="working-spinner"]')?.textContent
      expect(after).toBe(before)  // static glyph — no animation frames
    })
  })

  describe('phrase stability (motion allowed)', () => {
    it('keeps the same phrase for the whole working period (no periodic rotation)', () => {
      vi.useFakeTimers()
      render(<WorkingIndicator />)
      const initialWord = WORKING_WORDS.find((w) => document.body.textContent?.includes(w))
      expect(initialWord).toBeDefined()

      // Advance well past the old 2800ms rotation window many times over.
      act(() => { vi.advanceTimersByTime(30_000) })

      // The originally-chosen word is still the only one present.
      expect(document.body.textContent).toContain(initialWord!)
      const othersPresent = WORKING_WORDS.filter(
        (w) => w !== initialWord && document.body.textContent?.includes(w),
      )
      expect(othersPresent).toEqual([])
    })
  })

  describe('spinner animation (motion allowed)', () => {
    it('advances the braille spinner glyph over time', () => {
      vi.useFakeTimers()
      render(<WorkingIndicator />)
      const spinner = () => document.querySelector('[data-testid="working-spinner"]')?.textContent
      const frames = new Set<string>()
      for (let i = 0; i < 12; i++) {
        act(() => { vi.advanceTimersByTime(80) })
        frames.add(spinner() ?? '')
      }
      expect(frames.size).toBeGreaterThan(1)  // glyph actually cycles
    })
  })

  describe('sub-agent phrase', () => {
    it('shows the sub-agent phrase when subAgentCount > 0', () => {
      render(<WorkingIndicator subAgentCount={1} />)
      const sr = document.querySelector('.sr-only')
      expect(sr?.textContent).toBe('Running a sub-agent…')
    })

    it('pluralizes for multiple sub-agents', () => {
      render(<WorkingIndicator subAgentCount={3} />)
      const sr = document.querySelector('.sr-only')
      expect(sr?.textContent).toBe('Running 3 sub-agents…')
    })
  })

  describe('elapsed time readout', () => {
    it('shows seconds elapsed and updates as time passes', () => {
      vi.useFakeTimers()
      render(<WorkingIndicator />)
      act(() => { vi.advanceTimersByTime(12_000) })
      expect(screen.getByTestId('working-elapsed').textContent).toContain('12s')
    })

    it('formats past a minute as "Nm Ss"', async () => {
      const { formatElapsed } = await import('../elapsed')
      expect(formatElapsed(0)).toBe('0s')
      expect(formatElapsed(12_000)).toBe('12s')
      expect(formatElapsed(62_000)).toBe('1m 2s')
      expect(formatElapsed(125_000)).toBe('2m 5s')
    })
  })
})
