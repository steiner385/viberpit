import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { BulkActionBar } from '../BulkActionBar'

describe('BulkActionBar', () => {
  it('is a labelled region carrying the count text and its actions', () => {
    render(
      <BulkActionBar label="3 selected" onClear={vi.fn()}>
        <button type="button">Unarchive (3)</button>
      </BulkActionBar>,
    )
    expect(screen.getByRole('region', { name: 'Bulk actions' })).toBeInTheDocument()
    expect(screen.getByText('3 selected')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Unarchive (3)' })).toBeInTheDocument()
  })

  it('calls onClear from the clear control, whose label the caller names', async () => {
    const onClear = vi.fn()
    render(<BulkActionBar label="0 selected" clearLabel="Done" onClear={onClear} />)
    await userEvent.click(screen.getByRole('button', { name: 'Done' }))
    expect(onClear).toHaveBeenCalledOnce()
  })

  it('defaults the clear control to "Clear"', () => {
    render(<BulkActionBar label="1 selected" onClear={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Clear' })).toBeInTheDocument()
  })
})
