import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { ModelSwitchDivider } from '../ModelSwitchDivider'

describe('ModelSwitchDivider', () => {
  it('renders the target model name', () => {
    render(<ModelSwitchDivider to="Opus 4.8" />)
    expect(screen.getByText(/Opus 4\.8/)).toBeInTheDocument()
  })
  it('is a separator for a11y', () => {
    render(<ModelSwitchDivider to="Haiku 4.5" />)
    expect(screen.getByRole('separator')).toBeInTheDocument()
  })
})
