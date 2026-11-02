import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { SessionToolbar } from './SessionToolbar'

const base = {
  sessionId: 's1',
  status: { state: 'idle', count: 0 },
  connectionState: 'connected',
  subAgentCount: 0,
  viewMode: 'normal' as const,
  onSelectViewMode: () => {},
  rail: null,
}

describe('SessionToolbar', () => {
  it('holds the status, the view mode and the export — and nothing else', () => {
    const { container } = render(<SessionToolbar {...base} />)
    expect(screen.getByRole('status')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Export transcript' })).toBeInTheDocument()
    const buttons = screen.getAllByRole('button').map((b) => b.getAttribute('aria-label') ?? b.textContent)
    // View mode + export, no rail toggle without a rail.
    expect(buttons).toHaveLength(2)
    // The identity readouts moved to the rail cards.
    expect(screen.queryByLabelText('Switch account')).toBeNull()
    expect(screen.queryByLabelText(/account quota/i)).toBeNull()
    expect(screen.queryByRole('button', { name: /sub-agent/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /background work/i })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Session details' })).toBeNull()
    expect(container.querySelector('select')).toBeNull()
  })

  it('shows the rail toggle when there is a rail: Hide details while expanded', () => {
    const onToggle = vi.fn()
    render(<SessionToolbar {...base} rail={{ collapsed: false, onToggle, controls: 'rail-id', attention: false }} />)
    const toggle = screen.getByRole('button', { name: 'Hide details' })
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(toggle).toHaveAttribute('aria-controls', 'rail-id')
    fireEvent.click(toggle)
    expect(onToggle).toHaveBeenCalledTimes(1)
  })

  it('reads Show details while collapsed', () => {
    render(<SessionToolbar {...base} rail={{ collapsed: true, onToggle: () => {}, controls: 'rail-id', attention: false }} />)
    const toggle = screen.getByRole('button', { name: 'Show details' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    // Nothing to control while the rail is unmounted.
    expect(toggle).not.toHaveAttribute('aria-controls')
  })

  it('gives the rail toggle a 44px floor on coarse pointers', () => {
    render(<SessionToolbar {...base} rail={{ collapsed: false, onToggle: () => {}, controls: 'rail-id', attention: false }} />)
    expect(screen.getByRole('button', { name: 'Hide details' }))
      .toHaveClass('min-h-11', '[@media(hover:hover)]:min-h-0', 'min-w-11', '[@media(hover:hover)]:min-w-0')
  })

  it('puts the attention dot on the collapsed toggle and names it', () => {
    render(<SessionToolbar {...base} rail={{ collapsed: true, onToggle: () => {}, controls: 'rail-id', attention: true }} />)
    const toggle = screen.getByRole('button', { name: 'Show details (needs attention)' })
    expect(toggle.querySelector('[data-testid="rail-toggle-attention"]')).not.toBeNull()
  })

  it('shows no dot on an expanded rail (the cards show the cause) or without attention', () => {
    const { rerender } = render(<SessionToolbar {...base} rail={{ collapsed: false, onToggle: () => {}, controls: 'rail-id', attention: true }} />)
    expect(screen.getByRole('button', { name: 'Hide details' })).toBeInTheDocument()
    expect(screen.queryByTestId('rail-toggle-attention')).toBeNull()
    rerender(<SessionToolbar {...base} rail={{ collapsed: true, onToggle: () => {}, controls: 'rail-id', attention: false }} />)
    expect(screen.getByRole('button', { name: 'Show details' })).toBeInTheDocument()
    expect(screen.queryByTestId('rail-toggle-attention')).toBeNull()
  })
})
