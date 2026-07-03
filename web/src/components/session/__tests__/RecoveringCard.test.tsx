import { render, screen } from '@testing-library/react'
import { RecoveringCard } from '../RecoveringCard'

it('renders the recovering message without alert role or red styling', () => {
  render(<RecoveringCard message="Recovering the dropped turn…" />)
  expect(screen.getByText('Recovering the dropped turn…')).toBeInTheDocument()
  expect(screen.queryByRole('alert')).toBeNull()   // NOT an error surface
})
