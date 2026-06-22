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

describe('StatusBar', () => {
  it('shows "Ready" for idle state', () => {
    render(<StatusBar status={{ state: 'idle', count: 0 }} connectionState="connected" />)
    expect(screen.getByText('Ready')).toBeInTheDocument()
  })

  it('renders the WorkingIndicator for in_progress with no queue', () => {
    render(<StatusBar status={{ state: 'in_progress', count: 0 }} connectionState="connected" />)
    const srEl = document.querySelector('.sr-only')
    expect(srEl?.textContent).toBe('Working…')
  })

  it('renders WorkingIndicator with queued label for in_progress with queued items', () => {
    render(<StatusBar status={{ state: 'in_progress', count: 2 }} connectionState="connected" />)
    const srEl = document.querySelector('.sr-only')
    expect(srEl?.textContent).toBe('Working, 2 queued')
  })

  it('shows "Permission needed" for waiting_input', () => {
    render(<StatusBar status={{ state: 'waiting_input', count: 0 }} connectionState="connected" />)
    expect(screen.getByText('Permission needed')).toBeInTheDocument()
  })

  it('shows "Answer needed" for waiting_question', () => {
    render(<StatusBar status={{ state: 'waiting_question', count: 0 }} connectionState="connected" />)
    expect(screen.getByText('Answer needed')).toBeInTheDocument()
  })

  it('shows "Approve plan" for ready_review', () => {
    render(<StatusBar status={{ state: 'ready_review', count: 0 }} connectionState="connected" />)
    expect(screen.getByText('Approve plan')).toBeInTheDocument()
  })

  it('shows "Your turn" for done', () => {
    render(<StatusBar status={{ state: 'done', count: 0 }} connectionState="connected" />)
    expect(screen.getByText('Your turn')).toBeInTheDocument()
  })

  it('shows "Turn failed" for needs_attention', () => {
    render(<StatusBar status={{ state: 'needs_attention', count: 0 }} connectionState="connected" />)
    expect(screen.getByText('Turn failed')).toBeInTheDocument()
  })

  it('shows Reconnecting note when connectionState is reconnecting', () => {
    render(<StatusBar status={{ state: 'idle', count: 0 }} connectionState="reconnecting" />)
    expect(screen.getByText('Reconnecting…')).toBeInTheDocument()
  })

  it('shows Disconnected note when connectionState is disconnected', () => {
    render(<StatusBar status={{ state: 'idle', count: 0 }} connectionState="disconnected" />)
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
