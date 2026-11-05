import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { NumbersPanel } from './NumbersPanel'
import { NUMBERS } from './__fixtures__/ladder'

describe('NumbersPanel', () => {
  it('lists each number with its value and age', () => {
    render(<NumbersPanel numbers={NUMBERS} />)
    expect(screen.getByRole('heading', { name: 'Numbers' })).toBeInTheDocument()
    expect(screen.getByText('293')).toBeInTheDocument()
    expect(screen.getByText('17 of 27')).toBeInTheDocument()
    expect(screen.getByText('evidence cells missing in live capabilities')).toBeInTheDocument()
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it('renders nothing with no numbers', () => {
    const { container } = render(<NumbersPanel numbers={[]} />)
    expect(container).toBeEmptyDOMElement()
  })
})
