// The side panel's owner notes (spec 2026-11-05 §9): Needs you (open asks, the
// current milestone's first, each with Done and Drop) and Decisions (locked, then
// proposed; superseded collapsed). Each panel adds a note through one dialog.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { ApiRequestError } from '@/api/client'
import { NotesPanel } from './NotesPanel'
import { ladderView } from './__fixtures__/ladder'

const m = () => ({ mutate: vi.fn(), reset: vi.fn(), isPending: false, error: null as unknown })
const q = vi.hoisted(() => ({ add: null as unknown as ReturnType<typeof m>, update: null as unknown as ReturnType<typeof m> }))
vi.mock('@/api/milestones', async (orig) => ({
  ...(await orig<typeof import('@/api/milestones')>()),
  useAddNote: () => q.add,
  useUpdateNote: () => q.update,
}))

beforeEach(() => {
  q.add = m()
  q.update = m()
})

const view = ladderView()

describe('NotesPanel — Needs you', () => {
  it('lists open asks only, the current milestone first, with where each sits', () => {
    render(<NotesPanel kind="ask" slug="kindash" view={view} />)
    expect(screen.getByRole('heading', { name: 'Needs you' })).toBeInTheDocument()
    const items = within(screen.getByRole('list', { name: 'Open asks' })).getAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(items[0]).toHaveTextContent('Confirm the DPA with the payment processor')
    expect(items[0]).toHaveTextContent('Q2')
    expect(items[0]).toHaveTextContent('Phase 0 · Household gate')
    expect(items[1]).toHaveTextContent('Name the 3 to 5 friendly families')
    expect(items[1]).toHaveTextContent('Whole ladder')
    expect(screen.queryByText('Pick a launch city')).toBeNull()
  })

  it('Done and Drop resolve an ask', () => {
    render(<NotesPanel kind="ask" slug="kindash" view={view} />)
    fireEvent.click(screen.getByRole('button', { name: 'Done: Confirm the DPA with the payment processor' }))
    expect(q.update.mutate).toHaveBeenCalledWith({ id: 'a2', status: 'done' })
    fireEvent.click(screen.getByRole('button', { name: 'Drop: Name the 3 to 5 friendly families' }))
    expect(q.update.mutate).toHaveBeenCalledWith({ id: 'a1', status: 'dropped' })
  })

  it('says so when nothing needs you', () => {
    render(<NotesPanel kind="ask" slug="kindash" view={{ ...view, notes: { ...view.notes, asks: [] } }} />)
    expect(screen.getByText('Nothing needs you.')).toBeInTheDocument()
  })

  it('shows a refused update in plain words', () => {
    q.update.error = new ApiRequestError(403, { detail: { error: 'owner_required' } })
    render(<NotesPanel kind="ask" slug="kindash" view={view} />)
    expect(screen.getByRole('alert')).toHaveTextContent('Only the owner can change what someone else recorded')
  })

  it('Add opens a labelled form and posts an ask on a milestone', () => {
    q.add.mutate.mockImplementation((_v, opts) => opts?.onSuccess?.())
    render(<NotesPanel kind="ask" slug="kindash" view={view} />)
    fireEvent.click(screen.getByRole('button', { name: 'Add an ask' }))
    const dialog = screen.getByRole('dialog', { name: 'Add a note' })
    expect(within(dialog).getByLabelText(/Kind/)).toHaveValue('ask')
    const save = within(dialog).getByRole('button', { name: 'Add note' })
    expect(save).toBeDisabled()
    fireEvent.change(within(dialog).getByLabelText(/Text/), { target: { value: ' Book the kickoff call ' } })
    fireEvent.change(within(dialog).getByLabelText(/Reference/), { target: { value: 'Q3' } })
    fireEvent.change(within(dialog).getByLabelText(/Milestone/), { target: { value: 'friendly' } })
    fireEvent.click(save)
    expect(q.add.mutate).toHaveBeenCalledWith(
      { kind: 'ask', text: 'Book the kickoff call', ref: 'Q3', url: null, milestoneKey: 'friendly' }, expect.anything())
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})

describe('NotesPanel — Add limits', () => {
  it('caps the reference and text at the server limits', () => {
    render(<NotesPanel kind="ask" slug="kindash" view={view} />)
    fireEvent.click(screen.getByRole('button', { name: 'Add an ask' }))
    const dialog = screen.getByRole('dialog', { name: 'Add a note' })
    expect(within(dialog).getByLabelText(/Reference/)).toHaveAttribute('maxLength', '20')
    expect(within(dialog).getByLabelText(/Text/)).toHaveAttribute('maxLength', '500')
  })
})

describe('NotesPanel — Decisions', () => {
  it('lists locked then proposed, superseded collapsed', () => {
    render(<NotesPanel kind="decision" slug="kindash" view={view} />)
    expect(screen.getByRole('heading', { name: 'Decisions' })).toBeInTheDocument()
    const items = within(screen.getByRole('list', { name: 'Decisions' })).getAllByRole('listitem')
    expect(items.map((li) => li.textContent)).toEqual([
      expect.stringContaining('Lifetime free PLUS'),
      expect.stringContaining('Founder pricing'),
    ])
    expect(items[1]).toHaveTextContent('Proposed')
    const superseded = screen.getByText('Superseded (1)')
    expect(superseded.closest('details')).not.toHaveAttribute('open')
  })

  it('locks a proposed decision and supersedes a locked one', () => {
    render(<NotesPanel kind="decision" slug="kindash" view={view} />)
    fireEvent.click(screen.getByRole('button', { name: 'Lock: Founder pricing at $4/month' }))
    expect(q.update.mutate).toHaveBeenCalledWith({ id: 'd2', status: 'locked' })
    fireEvent.click(screen.getByRole('button', { name: 'Supersede: Lifetime free PLUS for friendly families' }))
    expect(q.update.mutate).toHaveBeenCalledWith({ id: 'd1', status: 'superseded' })
  })

  it('adds a proposed decision ladder-wide', () => {
    render(<NotesPanel kind="decision" slug="kindash" view={view} />)
    fireEvent.click(screen.getByRole('button', { name: 'Add a decision' }))
    const dialog = screen.getByRole('dialog', { name: 'Add a note' })
    fireEvent.change(within(dialog).getByLabelText(/Status/), { target: { value: 'proposed' } })
    fireEvent.change(within(dialog).getByLabelText(/Text/), { target: { value: 'Ship Android first' } })
    fireEvent.change(within(dialog).getByLabelText(/Link/), { target: { value: 'https://example.com/d' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add note' }))
    expect(q.add.mutate).toHaveBeenCalledWith(
      { kind: 'decision', text: 'Ship Android first', ref: null, url: 'https://example.com/d', milestoneKey: null, status: 'proposed' },
      expect.anything())
  })
})

describe('NotesPanel — note links', () => {
  it('links an http(s) note url', () => {
    render(<NotesPanel kind="decision" slug="kindash" view={view} />)
    expect(screen.getByRole('link', { name: 'Founder pricing at $4/month' }))
      .toHaveAttribute('href', 'https://github.com/cairnea/KinDash/issues/20300')
  })

  it.each(['javascript:alert(1)', 'data:text/html,<script>alert(1)</script>'])(
    'renders an unsafe url as plain text, never an anchor (%s)', (url) => {
      const asks = [{ ...view.notes.asks[0], url }]
      render(<NotesPanel kind="ask" slug="kindash" view={{ ...view, notes: { ...view.notes, asks } }} />)
      expect(screen.getByText(view.notes.asks[0].text)).toBeInTheDocument()
      expect(document.querySelector('a')).toBeNull()
    })
})
