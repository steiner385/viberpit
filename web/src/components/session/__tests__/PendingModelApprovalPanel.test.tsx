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

  // Finding 1: the positive case above proved nothing about the negative case —
  // a human/automation-initiated park must NOT be misattributed to the session.
  it('does not attribute a human-initiated request to the session', () => {
    render(<PendingModelApprovalPanel model="fable" requestedBy="tony@example.com"
                                      onDecision={vi.fn()} />)
    expect(screen.queryByText(/the session requested this/i)).toBeNull()
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

  // Finding 2: a pinned id (e.g. "claude-opus-5") is a valid request but is not
  // in the alias catalog useModels() returns — it must be prepended so the
  // default selection always exists.
  it('prepends a pinned id not in the alias catalog so the default selection exists', () => {
    render(<PendingModelApprovalPanel model="claude-opus-5" onDecision={vi.fn()} />)
    const select = screen.getByLabelText(/approve as/i) as HTMLSelectElement
    expect(select.value).toBe('claude-opus-5')
    const values = Array.from(select.querySelectorAll('option')).map((o) => o.value)
    expect(values).toContain('claude-opus-5')
  })

  it('does not duplicate a requested model that is already in the catalog', () => {
    render(<PendingModelApprovalPanel model="fable" onDecision={vi.fn()} />)
    const select = screen.getByLabelText(/approve as/i) as HTMLSelectElement
    const fableOptions = Array.from(select.querySelectorAll('option'))
      .filter((o) => o.value === 'fable')
    expect(fableOptions).toHaveLength(1)
  })

  // Finding 3: restored from the pre-Task-9 test file (git show b5459ddd), adapted
  // to the current props (onDecision now takes a second arg, unrelated here).
  it('renders a full model id via modelName', () => {
    render(<PendingModelApprovalPanel model="claude-opus-4-8" onDecision={vi.fn()} />)
    expect(screen.getByText(/Opus 4\.8/)).toBeInTheDocument()
  })

  it('falls back to "this model" when model is null', () => {
    render(<PendingModelApprovalPanel model={null} onDecision={vi.fn()} />)
    expect(screen.getByText(/this model/i)).toBeInTheDocument()
  })

  it('exposes role="region" with aria-live="polite"', () => {
    render(<PendingModelApprovalPanel model="fable" onDecision={vi.fn()} />)
    const region = screen.getByRole('region', { name: /pending model approval/i })
    expect(region).toHaveAttribute('aria-live', 'polite')
  })

  // Finding 4: an absent reason must render no quote markup at all.
  it('renders no reason quote when reason is absent', () => {
    const { container } = render(
      <PendingModelApprovalPanel model="fable" reason={null} onDecision={vi.fn()} />,
    )
    expect(container.textContent).not.toMatch(/[“”]/)
  })

  // Whole-branch review finding 4: the panel stays mounted while pendingModel is
  // truthy, so a standing hold can be overwritten by a DIFFERENT parked model (the
  // dedupe upstream only short-circuits on an identical model). The picker must
  // follow the new `model` prop, or a stale "Approve as" selection silently grants
  // a different (often costlier) model than the headline announces.
  it('follows the model prop when it changes on a re-render', () => {
    const onDecision = vi.fn()
    const { rerender } = render(
      <PendingModelApprovalPanel model="fable" onDecision={onDecision} />,
    )
    const select = screen.getByLabelText(/approve as/i) as HTMLSelectElement
    expect(select.value).toBe('fable')

    rerender(<PendingModelApprovalPanel model="opus" onDecision={onDecision} />)

    expect(select.value).toBe('opus')
    fireEvent.click(screen.getByRole('button', { name: /approve/i }))
    expect(onDecision).toHaveBeenCalledWith('approve', 'opus')
  })

  it('still respects a manual counter-offer picked after a model-prop change', () => {
    const onDecision = vi.fn()
    const { rerender } = render(
      <PendingModelApprovalPanel model="fable" onDecision={onDecision} />,
    )
    rerender(<PendingModelApprovalPanel model="opus" onDecision={onDecision} />)

    fireEvent.change(screen.getByLabelText(/approve as/i), { target: { value: 'haiku' } })
    fireEvent.click(screen.getByRole('button', { name: /approve/i }))
    expect(onDecision).toHaveBeenCalledWith('approve', 'haiku')
  })
})
