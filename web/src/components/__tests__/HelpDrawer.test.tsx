import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HelpDrawer } from '../HelpDrawer'
import { SHORTCUTS } from '@/lib/shortcuts'

describe('HelpDrawer', () => {
  it('is a labelled dialog with a row per registry shortcut (drift guard)', () => {
    render(<HelpDrawer onClose={vi.fn()} />)
    expect(screen.getByRole('dialog', { name: /help/i })).toBeInTheDocument()
    for (const s of SHORTCUTS) expect(screen.getAllByText(s.description).length).toBeGreaterThan(0)
  })
  it('shows list-mode keys and not the old Ctrl bindings', () => {
    render(<HelpDrawer onClose={vi.fn()} />)
    expect(screen.getByText('c')).toBeInTheDocument()
    expect(screen.getByText('a / Backspace')).toBeInTheDocument()
    expect(screen.queryByText('Ctrl+X')).not.toBeInTheDocument()
  })
  it('has Navigation and Tips sections', () => {
    render(<HelpDrawer onClose={vi.fn()} />)
    expect(screen.getByRole('heading', { name: /navigation/i })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /tips/i })).toBeInTheDocument()
  })
  it('Escape and backdrop click close', async () => {
    const onClose = vi.fn()
    const user = userEvent.setup()
    render(<HelpDrawer onClose={onClose} />)
    await user.keyboard('{Escape}')
    await user.click(screen.getByTestId('help-drawer-backdrop'))
    expect(onClose).toHaveBeenCalledTimes(2)
  })
})
