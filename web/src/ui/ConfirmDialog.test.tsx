import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ConfirmDialog } from './ConfirmDialog'

describe('ConfirmDialog', () => {
  it('confirm tier: Cancel closes without confirming; the action button confirms', async () => {
    const onClose = vi.fn(); const onConfirm = vi.fn()
    render(<ConfirmDialog open onClose={onClose} onConfirm={onConfirm} title="Delete hook?" description="It runs on every session." confirmLabel="Delete hook" />)
    expect(screen.getByRole('dialog', { name: 'Delete hook?' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onClose).toHaveBeenCalled()
    expect(onConfirm).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Delete hook' }))
    expect(onConfirm).toHaveBeenCalledTimes(1)
  })

  it('typed tier: the action stays disabled until the exact name is typed', async () => {
    const onConfirm = vi.fn()
    render(<ConfirmDialog open onClose={() => {}} onConfirm={onConfirm} title="Delete channel “ntfy-phone”?" description="3 rules go with it." confirmLabel="Delete channel" typed="ntfy-phone" />)
    const btn = screen.getByRole('button', { name: 'Delete channel' })
    expect(btn).toBeDisabled()
    await userEvent.type(screen.getByLabelText(/Type ntfy-phone to confirm/), 'ntfy-phon')
    expect(btn).toBeDisabled()
    await userEvent.type(screen.getByLabelText(/Type ntfy-phone to confirm/), 'e')
    expect(btn).toBeEnabled()
    await userEvent.click(btn)
    expect(onConfirm).toHaveBeenCalled()
  })
})
