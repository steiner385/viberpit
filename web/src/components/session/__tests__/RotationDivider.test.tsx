import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { RotationDivider } from '../RotationDivider'

describe('RotationDivider', () => {
  it('renders the switch text', () => {
    render(<RotationDivider from="alice" to="bob" />)
    expect(screen.getByText(/switched alice → bob/)).toBeInTheDocument()
  })

  it('includes reason when provided', () => {
    render(<RotationDivider from="alice" to="bob" reason="rate limited" />)
    expect(screen.getByText(/switched alice → bob — rate limited/)).toBeInTheDocument()
  })

  it('omits reason when not provided', () => {
    render(<RotationDivider from="alice" to="bob" />)
    expect(screen.queryByText(/—/)).not.toBeInTheDocument()
  })

  it('has role="separator" and correct aria-label', () => {
    render(<RotationDivider from="alice" to="bob" />)
    const el = screen.getByRole('separator')
    expect(el).toBeInTheDocument()
    expect(el).toHaveAttribute('aria-label', 'Account switched from alice to bob')
  })
})
