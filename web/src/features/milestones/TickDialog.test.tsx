// Ticking a manual condition (spec 2026-11-05 §9 actions, §10 PUT): Met / Not met
// for a gate condition, a number for a numbers entry; evidence is required, a link
// optional. A refusal shows the server's code as plain words.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { ApiRequestError } from '@/api/client'
import { TickDialog } from './TickDialog'
import { cond } from './__fixtures__/ladder'

const q = vi.hoisted(() => ({
  tick: { mutate: vi.fn(), reset: vi.fn(), isPending: false, error: null as unknown },
}))
vi.mock('@/api/milestones', async (orig) => ({
  ...(await orig<typeof import('@/api/milestones')>()),
  useTickCondition: () => q.tick,
}))

beforeEach(() => {
  q.tick = { mutate: vi.fn(), reset: vi.fn(), isPending: false, error: null }
})

const setup = cond({ key: 'setup', label: 'Every family completes setup' })

describe('TickDialog', () => {
  it('ticks a gate condition Met with evidence and a link, then closes', () => {
    const onClose = vi.fn()
    q.tick.mutate.mockImplementation((_v, opts) => opts?.onSuccess?.())
    render(<TickDialog open onClose={onClose} slug="kindash" scope="friendly" condition={setup} />)
    const dialog = screen.getByRole('dialog', { name: 'Tick condition' })
    expect(dialog).toHaveTextContent('Every family completes setup')
    const submit = screen.getByRole('button', { name: 'Save tick' })
    expect(submit).toBeDisabled()
    fireEvent.change(screen.getByLabelText(/Reading/), { target: { value: 'met' } })
    fireEvent.change(screen.getByLabelText(/Evidence/), { target: { value: '  call notes 11/04 ' } })
    fireEvent.change(screen.getByLabelText(/Link/), { target: { value: 'https://example.com/notes' } })
    expect(submit).toBeEnabled()
    fireEvent.click(submit)
    expect(q.tick.mutate).toHaveBeenCalledWith(
      { scope: 'friendly', conditionKey: 'setup', met: true, evidence: 'call notes 11/04', url: 'https://example.com/notes' },
      expect.anything(),
    )
    expect(onClose).toHaveBeenCalled()
  })

  it('unticks with Not met; the reading starts at the stored one', () => {
    render(<TickDialog open onClose={() => {}} slug="kindash" scope="friendly"
      condition={{ ...setup, value: { met: true }, manualBy: 'tony', manualEvidence: 'old' }} />)
    expect(screen.getByLabelText(/Reading/)).toHaveValue('met')
    expect(screen.getByLabelText(/Evidence/)).toHaveValue('old')
    expect(screen.getByLabelText(/Link/)).toHaveValue('')
    fireEvent.change(screen.getByLabelText(/Reading/), { target: { value: 'unmet' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save tick' }))
    expect(q.tick.mutate).toHaveBeenCalledWith(
      expect.objectContaining({ met: false, evidence: 'old', url: null }), expect.anything())
  })

  it('a numbers entry takes a number instead of Met / Not met', () => {
    render(<TickDialog open onClose={() => {}} slug="kindash" scope="numbers"
      condition={cond({ key: 'outside-families', label: 'outside families on production' })} />)
    expect(screen.queryByLabelText(/Reading/)).toBeNull()
    fireEvent.change(screen.getByLabelText(/Value/), { target: { value: '12' } })
    fireEvent.change(screen.getByLabelText(/Evidence/), { target: { value: 'signup sheet' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save tick' }))
    expect(q.tick.mutate).toHaveBeenCalledWith(
      { scope: 'numbers', conditionKey: 'outside-families', value: 12, evidence: 'signup sheet', url: null },
      expect.anything())
  })

  it('shows a refusal in plain words', () => {
    q.tick.error = new ApiRequestError(403, { detail: { error: 'owner_required', tickedBy: 'sam' } })
    render(<TickDialog open onClose={() => {}} slug="kindash" scope="friendly" condition={setup} />)
    expect(screen.getByRole('alert')).toHaveTextContent('Only the owner can change what someone else recorded')
  })

  it('Update keeps the stored link when only the evidence changes', () => {
    render(<TickDialog open onClose={() => {}} slug="kindash" scope="friendly"
      condition={{ ...setup, value: { met: true }, manualBy: 'tony', manualEvidence: 'old', manualUrl: 'https://e.x/proof' }} />)
    expect(screen.getByLabelText(/Link/)).toHaveValue('https://e.x/proof')
    fireEvent.change(screen.getByLabelText(/Evidence/), { target: { value: 'newer notes' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save tick' }))
    expect(q.tick.mutate).toHaveBeenCalledWith(
      expect.objectContaining({ met: true, evidence: 'newer notes', url: 'https://e.x/proof' }), expect.anything())
  })

  it('caps the evidence at the server limit', () => {
    render(<TickDialog open onClose={vi.fn()} slug="kindash" scope="friendly" condition={setup} />)
    expect(screen.getByLabelText(/Evidence/)).toHaveAttribute('maxLength', '500')
  })
})
