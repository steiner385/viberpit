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

  it('renders the WorkingIndicator for working state with no queue', () => {
    render(<StatusBar status={{ state: 'working', count: 0 }} connectionState="connected" />)
    // WorkingIndicator always provides a sr-only "Working…" label
    const srEl = document.querySelector('.sr-only')
    expect(srEl).not.toBeNull()
    expect(srEl?.textContent).toBe('Working…')
  })

  it('renders WorkingIndicator with queued label for working state with queued items', () => {
    render(<StatusBar status={{ state: 'working', count: 2 }} connectionState="connected" />)
    const srEl = document.querySelector('.sr-only')
    expect(srEl).not.toBeNull()
    expect(srEl?.textContent).toBe('Working, 2 queued')
  })

  it('shows "Waiting for permission" for waiting-permission state', () => {
    render(<StatusBar status={{ state: 'waiting-permission', count: 0 }} connectionState="connected" />)
    expect(screen.getByText('Waiting for permission')).toBeInTheDocument()
  })

  it('shows count for queued state', () => {
    render(<StatusBar status={{ state: 'queued', count: 3 }} connectionState="connected" />)
    expect(screen.getByText('3 queued')).toBeInTheDocument()
  })

  it('shows raw state string for unknown state', () => {
    render(<StatusBar status={{ state: 'custom-state', count: 0 }} connectionState="connected" />)
    expect(screen.getByText('custom-state')).toBeInTheDocument()
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
