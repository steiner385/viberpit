import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { PermanentDeleteDialog } from '../PermanentDeleteDialog'

const base = { onClose: vi.fn(), onConfirm: vi.fn(), isPending: false }

describe('PermanentDeleteDialog', () => {
  it('names the single session in the title and the body', () => {
    render(<PermanentDeleteDialog {...base} names={['My Session']} />)
    expect(screen.getByText('Permanently delete session')).toBeInTheDocument()
    expect(screen.getByText(/My Session/)).toBeInTheDocument()
  })

  it('counts the sessions when several are named', () => {
    render(<PermanentDeleteDialog {...base} names={['a', 'b', 'c', 'd']} />)
    expect(screen.getByText('Permanently delete 4 sessions')).toBeInTheDocument()
  })

  it('starts focus on Cancel and closes from it', async () => {
    const onClose = vi.fn()
    render(<PermanentDeleteDialog {...base} onClose={onClose} names={['One']} />)
    const cancel = screen.getByRole('button', { name: 'Cancel' })
    expect(cancel).toHaveFocus()
    await userEvent.click(cancel)
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('shows a progressbar and routes Cancel to the abort while a bulk run is in flight', async () => {
    const onCancel = vi.fn()
    const onClose = vi.fn()
    render(
      <PermanentDeleteDialog
        {...base}
        names={['a', 'b', 'c']}
        isPending
        progress={{ done: 1, total: 3 }}
        onCancel={onCancel}
        onClose={onClose}
      />,
    )
    const bar = screen.getByRole('progressbar')
    expect(bar).toHaveAttribute('aria-valuenow', '1')
    expect(bar).toHaveAttribute('aria-valuemax', '3')
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onCancel).toHaveBeenCalledOnce()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('refuses to dismiss via Escape while a bulk run (with an abort path) is in flight', async () => {
    const onClose = vi.fn()
    render(
      <PermanentDeleteDialog
        {...base}
        names={['a', 'b', 'c']}
        isPending
        progress={{ done: 1, total: 3 }}
        onCancel={vi.fn()}
        onClose={onClose}
      />,
    )
    await userEvent.keyboard('{Escape}')
    expect(onClose).not.toHaveBeenCalled()
  })

  it('still dismisses via Escape while pending on the single-session path (no onCancel)', async () => {
    const onClose = vi.fn()
    render(<PermanentDeleteDialog {...base} names={['One']} isPending onClose={onClose} />)
    await userEvent.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('confirms', async () => {
    const onConfirm = vi.fn()
    render(<PermanentDeleteDialog {...base} onConfirm={onConfirm} names={['One']} />)
    await userEvent.click(screen.getByRole('button', { name: 'Delete permanently' }))
    expect(onConfirm).toHaveBeenCalledOnce()
  })
})
