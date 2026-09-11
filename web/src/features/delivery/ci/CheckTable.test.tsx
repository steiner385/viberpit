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
  it('shows the empty message when there are no rows', () => {
    render(<CheckTable title="Flakiest" empty="No flaky checks"
      columns={[{ header: 'Check', cell: (r: { name: string }) => r.name }]} rows={[]} />)
    expect(screen.getByText('No flaky checks')).toBeInTheDocument()
  })
})
