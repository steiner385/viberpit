import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { StatusBar } from '../StatusBar'

// Stub matchMedia so WorkingIndicator works in jsdom
beforeEach(() => {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    configurable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  })
})

describe('StatusBar (rich tiers)', () => {
  it('shows "Ready" for the never-ran idle state (your-turn)', () => {
    render(<StatusBar status={{ state: 'idle', count: 0 }} connectionState="connected" />)
    expect(screen.getByText('Ready')).toBeInTheDocument()
  })

  it('shows "Your turn" for a finished turn (done)', () => {
    render(<StatusBar status={{ state: 'done', count: 0 }} connectionState="connected" />)
    expect(screen.getByText('Your turn')).toBeInTheDocument()
  })

  it('renders the WorkingIndicator for the busy tier (in_progress)', () => {
    render(<StatusBar status={{ state: 'in_progress', count: 0 }} connectionState="connected" />)
    const srEl = document.querySelector('.sr-only')
    expect(srEl).not.toBeNull()
    expect(srEl?.textContent).toBe('Working…')
  })

  it('still renders the WorkingIndicator for the legacy "working" alias', () => {
    render(<StatusBar status={{ state: 'working', count: 2 }} connectionState="connected" />)
    const srEl = document.querySelector('.sr-only')
    expect(srEl?.textContent).toBe('Working, 2 queued')
  })

  it('shows the needs-you label for waiting_input (amber)', () => {
    const { container } = render(
      <StatusBar status={{ state: 'waiting_input', count: 0 }} connectionState="connected" />,
    )
    expect(screen.getByText('Permission needed')).toBeInTheDocument()
    expect(container.querySelector('.text-amber-700')).toBeTruthy()
  })

  it('shows "Approve plan" for ready_review (needs-you)', () => {
    render(<StatusBar status={{ state: 'ready_review', count: 0 }} connectionState="connected" />)
    expect(screen.getByText('Approve plan')).toBeInTheDocument()
  })

  it('shows the error label for needs_attention (red)', () => {
    const { container } = render(
      <StatusBar status={{ state: 'needs_attention', count: 0 }} connectionState="connected" />,
    )
    expect(screen.getByText('Turn failed')).toBeInTheDocument()
    expect(container.querySelector('.text-red-700')).toBeTruthy()
  })

  it('renders a quiet "…" placeholder for the empty replaying state, never "Ready"', () => {
    render(<StatusBar status={{ state: '', count: 0 }} connectionState="connected" />)
    expect(screen.getByText('…')).toBeInTheDocument()
    expect(screen.queryByText('Ready')).not.toBeInTheDocument()
  })

  it('shows Reconnecting note when connectionState is reconnecting', () => {
    render(<StatusBar status={{ state: 'idle', count: 0 }} connectionState="reconnecting" />)
    expect(screen.getByText('Reconnecting…')).toBeInTheDocument()
  })

  it('shows Disconnected note when connectionState is disconnected', () => {
    render(<StatusBar status={{ state: 'idle', count: 0 }} connectionState="disconnected" />)
    expect(screen.getByText('Disconnected')).toBeInTheDocument()
  })

  it('keeps the last tier on disconnect (does not flip to a Ready-like state)', () => {
    // A busy session that disconnects keeps showing the working indicator + the note.
    render(<StatusBar status={{ state: 'in_progress', count: 0 }} connectionState="disconnected" />)
    const srEl = document.querySelector('.sr-only')
    expect(srEl?.textContent).toBe('Working…')
    expect(screen.getByText('Disconnected')).toBeInTheDocument()
  })

  it('shows no connection note when connected', () => {
    render(<StatusBar status={{ state: 'idle', count: 0 }} connectionState="connected" />)
    expect(screen.queryByText('Reconnecting…')).not.toBeInTheDocument()
    expect(screen.queryByText('Disconnected')).not.toBeInTheDocument()
  })

  it('has role="status" and aria-live="polite"', () => {
    render(<StatusBar status={{ state: 'idle', count: 0 }} connectionState="connected" />)
    const el = screen.getByRole('status')
    expect(el).toBeInTheDocument()
    expect(el).toHaveAttribute('aria-live', 'polite')
  })
})
