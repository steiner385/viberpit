import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ShortcutsHelp } from '../ShortcutsHelp'

describe('ShortcutsHelp', () => {
  it('renders as a labelled modal dialog', () => {
    render(<ShortcutsHelp onClose={vi.fn()} />)
    const dialog = screen.getByRole('dialog', { name: /keyboard shortcuts/i })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
  })

  it('lists core bindings', () => {
    render(<ShortcutsHelp onClose={vi.fn()} />)
    expect(screen.getByText(/navigate sessions/i)).toBeInTheDocument()
    expect(screen.getByText(/rename/i)).toBeInTheDocument()
    expect(screen.getByText(/cycle permission mode/i)).toBeInTheDocument()
  })

  it('Escape closes', async () => {
    const user = userEvent.setup()
    const onClose = vi.fn()
    render(<ShortcutsHelp onClose={onClose} />)
    await user.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalled()
  })

  it('backdrop click closes', async () => {
    const user = userEvent.setup()
    const onClose = vi.fn()
    render(<ShortcutsHelp onClose={onClose} />)
    // Click the backdrop (the outer dialog div itself)
    await user.click(screen.getByRole('dialog'))
    expect(onClose).toHaveBeenCalled()
  })

  it('focuses the close button on mount', () => {
    render(<ShortcutsHelp onClose={vi.fn()} />)
    expect(document.activeElement).toBe(screen.getByRole('button', { name: /close/i }))
  })
})
