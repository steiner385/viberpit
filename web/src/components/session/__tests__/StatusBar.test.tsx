import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { StatusBar } from '../StatusBar'

describe('StatusBar', () => {
  it('shows "Ready" for idle state', () => {
    render(<StatusBar status={{ state: 'idle', count: 0 }} connectionState="connected" />)
    expect(screen.getByText('Ready')).toBeInTheDocument()
  })

  it('shows "Working…" for working state with no queue', () => {
    render(<StatusBar status={{ state: 'working', count: 0 }} connectionState="connected" />)
    expect(screen.getByText('Working…')).toBeInTheDocument()
  })

  it('shows "Working… (N queued)" for working state with queued items', () => {
    render(<StatusBar status={{ state: 'working', count: 2 }} connectionState="connected" />)
    expect(screen.getByText('Working… (2 queued)')).toBeInTheDocument()
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
