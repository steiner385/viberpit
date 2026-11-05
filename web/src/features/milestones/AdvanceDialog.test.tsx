// Advancing the ladder (spec 2026-11-05 §7, §9 actions): a met gate advances on a
// confirm; an unmet one lists what is unmet or unknown and needs a reason of at
// least 10 characters. The owner rule and a concurrent advance answer in words.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { ApiRequestError } from '@/api/client'
import type { AdvanceMutation } from '@/api/milestones'
import { AdvanceDialog } from './AdvanceDialog'
import { FRIENDLY, HOUSEHOLD } from './__fixtures__/ladder'

const q = vi.hoisted(() => ({
  advance: { mutate: vi.fn(), reset: vi.fn(), isPending: false, error: null as unknown },
}))

beforeEach(() => {
  q.advance = { mutate: vi.fn(), reset: vi.fn(), isPending: false, error: null }
})

const mount = (over = {}) => render(
  <AdvanceDialog open onClose={() => {}} advance={q.advance as unknown as AdvanceMutation}
    milestone={HOUSEHOLD} next={FRIENDLY} {...over} />)

describe('AdvanceDialog', () => {
  it('with an unmet gate: lists the unmet and unknown conditions and needs a 10-character reason', () => {
    const onClose = vi.fn()
    q.advance.mutate.mockImplementation((_v, opts) => opts?.onSuccess?.())
    mount({ onClose })
    const dialog = screen.getByRole('dialog', { name: 'Advance with override' })
    const unmet = within(dialog).getByRole('list', { name: 'Not met yet' })
    expect(within(unmet).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      expect.stringContaining('Evidence rubric satisfied'),
      expect.stringContaining('Nine lane-integrity fixes'),
      expect.stringContaining('Nightly full suite'),
    ])
    expect(dialog).toHaveTextContent('Phase 1 · Friendly families')
    const submit = within(dialog).getByRole('button', { name: 'Advance with override' })
    const reason = within(dialog).getByLabelText(/Reason/)
    fireEvent.change(reason, { target: { value: 'too short' } })
    expect(submit).toBeDisabled()
    fireEvent.change(reason, { target: { value: '  owner accepted the risk  ' } })
    expect(submit).toBeEnabled()
    fireEvent.click(submit)
    expect(q.advance.mutate).toHaveBeenCalledWith(
      { expectedCurrent: 'household', overrideReason: 'owner accepted the risk' }, expect.anything())
    expect(onClose).toHaveBeenCalled()
  })

  it('with a met gate: a plain Advance, no reason field', () => {
    mount({ milestone: { ...HOUSEHOLD, gateSummary: { met: 4, total: 4, unknown: 0, ready: true },
      gate: HOUSEHOLD.gate.map((c) => ({ ...c, status: 'met' as const })) } })
    const dialog = screen.getByRole('dialog', { name: 'Advance' })
    expect(within(dialog).queryByLabelText(/Reason/)).toBeNull()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Advance' }))
    expect(q.advance.mutate).toHaveBeenCalledWith({ expectedCurrent: 'household' }, expect.anything())
  })

  it('a 403 says only the owner can override from the app', () => {
    q.advance.error = new ApiRequestError(403, { detail: { error: 'override_requires_owner' } })
    mount()
    expect(screen.getByRole('alert')).toHaveTextContent('Only the owner can override from the ViberPit app')
  })

  it('a 409 stale_current says someone else advanced the ladder', () => {
    q.advance.error = new ApiRequestError(409, { detail: { error: 'stale_current', current: 'friendly' } })
    mount()
    expect(screen.getByRole('alert')).toHaveTextContent('Someone else advanced this ladder')
  })

  it('the last rung says the ladder completes', () => {
    mount({ next: null })
    expect(screen.getByRole('dialog')).toHaveTextContent('This completes the ladder.')
  })
})
