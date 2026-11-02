import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { DiffChip } from './DiffChip'

function Where() {
  const loc = useLocation()
  return <div data-testid="where">{loc.search}</div>
}

function renderChip(additions: number, deletions: number, truncated = false) {
  return render(
    <MemoryRouter initialEntries={['/sessions/s1?view=conversation']}>
      <DiffChip additions={additions} deletions={deletions} truncated={truncated} />
      <Where />
    </MemoryRouter>,
  )
}

describe('DiffChip', () => {
  it('renders nothing with no changes', () => {
    renderChip(0, 0)
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('shows +N −M in the success/danger tokens', () => {
    renderChip(12, 1)
    const chip = screen.getByRole('button', { name: /open changes/i })
    expect(chip).toHaveTextContent('+12 −1')
    expect(screen.getByText('+12')).toHaveClass('text-success')
    expect(screen.getByText('−1')).toHaveClass('text-danger')
  })

  it('names the counts for a screen reader', () => {
    renderChip(12, 1)
    expect(screen.getByRole('button', { name: '12 additions, 1 deletion — open Changes' })).toBeInTheDocument()
  })

  it('marks truncated totals as lower bounds', () => {
    renderChip(3, 0, true)
    expect(screen.getByRole('button', { name: /at least 3 additions/i })).toBeInTheDocument()
  })

  it('opens the Changes tab', async () => {
    renderChip(12, 1)
    await userEvent.click(screen.getByRole('button', { name: /open changes/i }))
    expect(screen.getByTestId('where')).toHaveTextContent('?view=changes')
  })
})
