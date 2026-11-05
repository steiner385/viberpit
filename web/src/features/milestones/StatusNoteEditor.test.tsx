// The current milestone's one-line status, edited in place (spec 2026-11-05 §9;
// server cap STATUS_NOTE_MAX = 200). Empty clears it.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { ApiRequestError } from '@/api/client'
import { StatusNoteEditor } from './StatusNoteEditor'
import { HOUSEHOLD } from './__fixtures__/ladder'

const q = vi.hoisted(() => ({
  save: { mutate: vi.fn(), reset: vi.fn(), isPending: false, error: null as unknown },
}))
vi.mock('@/api/milestones', async (orig) => ({
  ...(await orig<typeof import('@/api/milestones')>()),
  useSetStatusNote: () => q.save,
}))

beforeEach(() => {
  q.save = { mutate: vi.fn(), reset: vi.fn(), isPending: false, error: null }
})

describe('StatusNoteEditor', () => {
  it('shows the line with an edit button; Save sends the trimmed text and returns to view', () => {
    q.save.mutate.mockImplementation((_v, opts) => opts?.onSuccess?.())
    render(<StatusNoteEditor slug="kindash" milestone={HOUSEHOLD} />)
    expect(screen.getByText('Current · the long phase by design')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Edit status line' }))
    const input = screen.getByRole('textbox', { name: 'Status line' })
    expect(input).toHaveValue('the long phase by design')
    expect(input).toHaveAttribute('maxLength', '200')
    fireEvent.change(input, { target: { value: '  waiting on the DPA  ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(q.save.mutate).toHaveBeenCalledWith({ key: 'household', statusNote: 'waiting on the DPA' }, expect.anything())
    expect(screen.queryByRole('textbox')).toBeNull()
  })

  it('an empty line clears it; Escape cancels', () => {
    render(<StatusNoteEditor slug="kindash" milestone={HOUSEHOLD} />)
    fireEvent.click(screen.getByRole('button', { name: 'Edit status line' }))
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Escape' })
    expect(screen.queryByRole('textbox')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Edit status line' }))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '   ' } })
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' })
    expect(q.save.mutate).toHaveBeenCalledWith({ key: 'household', statusNote: null }, expect.anything())
  })

  it('shows a refusal in plain words and stays open', () => {
    q.save.error = new ApiRequestError(400, { detail: { error: 'too_long', max: 200 } })
    render(<StatusNoteEditor slug="kindash" milestone={HOUSEHOLD} />)
    fireEvent.click(screen.getByRole('button', { name: 'Edit status line' }))
    expect(screen.getByRole('alert')).toHaveTextContent('That text is too long')
  })
})
