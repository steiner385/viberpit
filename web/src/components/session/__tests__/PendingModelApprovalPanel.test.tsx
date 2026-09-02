import { render, screen, fireEvent } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { PendingModelApprovalPanel } from '../PendingModelApprovalPanel'

vi.mock('@/api/hooks', () => ({
  useModels: () => ({ data: [{ id: 'haiku', label: 'Haiku' }, { id: 'sonnet', label: 'Sonnet' },
                             { id: 'opus', label: 'Opus' }, { id: 'fable', label: 'Fable' }] }),
}))

describe('PendingModelApprovalPanel', () => {
  it('shows the requested model label', () => {
    render(<PendingModelApprovalPanel model="fable" onDecision={vi.fn()} />)
    expect(screen.getByText(/Fable/)).toBeInTheDocument()
  })

  it('shows the reason when one is given', () => {
    render(<PendingModelApprovalPanel model="fable" reason="cross-module refactor"
                                      onDecision={vi.fn()} />)
    expect(screen.getByText(/cross-module refactor/)).toBeInTheDocument()
  })

  it('attributes a session-initiated request', () => {
    render(<PendingModelApprovalPanel model="fable" requestedBy="session:self"
                                      onDecision={vi.fn()} />)
    expect(screen.getByText(/the session requested this/i)).toBeInTheDocument()
  })

  it('approves the requested model when the picker is untouched', () => {
    const onDecision = vi.fn()
    render(<PendingModelApprovalPanel model="fable" onDecision={onDecision} />)
    fireEvent.click(screen.getByRole('button', { name: /approve/i }))
    expect(onDecision).toHaveBeenCalledWith('approve', 'fable')
  })

  it('approves the counter-offer when a different model is picked', () => {
    const onDecision = vi.fn()
    render(<PendingModelApprovalPanel model="fable" onDecision={onDecision} />)
    fireEvent.change(screen.getByLabelText(/approve as/i), { target: { value: 'opus' } })
    fireEvent.click(screen.getByRole('button', { name: /approve/i }))
    expect(onDecision).toHaveBeenCalledWith('approve', 'opus')
  })

  it('denies without a model', () => {
    const onDecision = vi.fn()
    render(<PendingModelApprovalPanel model="fable" onDecision={onDecision} />)
    fireEvent.click(screen.getByRole('button', { name: /deny/i }))
    expect(onDecision).toHaveBeenCalledWith('deny', undefined)
  })
})
