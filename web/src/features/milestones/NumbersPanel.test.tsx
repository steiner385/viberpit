import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { NumbersPanel } from './NumbersPanel'
import { NUMBERS } from './__fixtures__/ladder'

vi.mock('@/api/milestones', async (orig) => ({
  ...(await orig<typeof import('@/api/milestones')>()),
  useTickCondition: () => ({ mutate: vi.fn(), reset: vi.fn(), isPending: false, error: null }),
}))

describe('NumbersPanel', () => {
  it('a manual number gets a Tick that opens the dialog with a number field', () => {
    render(<NumbersPanel numbers={NUMBERS} slug="kindash" />)
    // Only the manual entries (outside-families, defects-by-ci); measured ones have no button.
    expect(screen.getAllByRole('button').map((b) => b.getAttribute('aria-label'))).toEqual([
      'Tick: outside families on production', 'Tick: of recent defects found by CI (%)'])
    fireEvent.click(screen.getByRole('button', { name: 'Tick: outside families on production' }))
    expect(screen.getByLabelText(/Value/)).toBeInTheDocument()
  })


  it('lists each number with its value and age', () => {
    render(<NumbersPanel numbers={NUMBERS} slug="kindash" />)
    expect(screen.getByRole('heading', { name: 'Numbers' })).toBeInTheDocument()
    expect(screen.getByText('293')).toBeInTheDocument()
    expect(screen.getByText('17 of 27')).toBeInTheDocument()
    expect(screen.getByText('evidence cells missing in live capabilities')).toBeInTheDocument()
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it('renders nothing with no numbers', () => {
    const { container } = render(<NumbersPanel numbers={[]} slug="kindash" />)
    expect(container).toBeEmptyDOMElement()
  })
})
