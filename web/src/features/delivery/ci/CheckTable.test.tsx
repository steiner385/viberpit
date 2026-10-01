import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { CheckTable } from './CheckTable'

describe('CheckTable', () => {
  it('renders title, headers and rows', () => {
    render(<CheckTable title="Slowest" empty="none"
      columns={[{ header: 'Check', cell: (r: { name: string }) => r.name }]}
      rows={[{ name: 'build' }, { name: 'e2e' }]} />)
    expect(screen.getByText('Slowest')).toBeInTheDocument()
    expect(screen.getByText('Check')).toBeInTheDocument()
    expect(screen.getByText('build')).toBeInTheDocument()
  })
  // 2026-10-01 Delivery re-critique, P1: a six-column table pushed the page
  // scroller 69px sideways on a 390px phone. The table scrolls in its section.
  it('scrolls a wide table inside its own section, never the page', () => {
    render(<CheckTable title="Regressions" empty="none"
      columns={[{ header: 'Check', cell: (r: { name: string }) => r.name }]}
      rows={[{ name: 'build' }]} />)
    expect(screen.getByRole('table', { name: 'Regressions' }).parentElement!.className).toMatch(/\boverflow-x-auto\b/)
  })

  it('shows the empty message when there are no rows', () => {
    render(<CheckTable title="Flakiest" empty="No flaky checks"
      columns={[{ header: 'Check', cell: (r: { name: string }) => r.name }]} rows={[]} />)
    expect(screen.getByText('No flaky checks')).toBeInTheDocument()
  })
})
