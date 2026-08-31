import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { PendingModelApprovalPanel } from '../PendingModelApprovalPanel'

describe('PendingModelApprovalPanel', () => {
  it('renders the pending model name and fires decisions', () => {
    const onDecision = vi.fn()
    render(<PendingModelApprovalPanel model="fable" onDecision={onDecision} />)
    // "fable" short alias is capitalized to "Fable"
    expect(screen.getByText(/Fable/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /approve/i }))
    expect(onDecision).toHaveBeenCalledWith('approve')
  })

  it('fires deny when the deny button is clicked', () => {
    const onDecision = vi.fn()
    render(<PendingModelApprovalPanel model="fable" onDecision={onDecision} />)
    fireEvent.click(screen.getByRole('button', { name: /deny/i }))
    expect(onDecision).toHaveBeenCalledWith('deny')
  })

  it('renders a full model id via modelName', () => {
    const onDecision = vi.fn()
    render(<PendingModelApprovalPanel model="claude-opus-4-8" onDecision={onDecision} />)
    expect(screen.getByText(/Opus 4\.8/)).toBeInTheDocument()
  })

  it('falls back to "this model" when model is null', () => {
    const onDecision = vi.fn()
    render(<PendingModelApprovalPanel model={null} onDecision={onDecision} />)
    expect(screen.getByText(/this model/i)).toBeInTheDocument()
  })

  it('exposes role="region" with aria-live="polite"', () => {
    render(<PendingModelApprovalPanel model="fable" onDecision={vi.fn()} />)
    const region = screen.getByRole('region', { name: /pending model approval/i })
    expect(region).toHaveAttribute('aria-live', 'polite')
  })
})
