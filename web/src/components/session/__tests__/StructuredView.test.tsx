import { describe, it, expect } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { StructuredView } from '../StructuredView'

describe('StructuredView', () => {
  it('renders a flat object as key/value rows', () => {
    render(<StructuredView data={{ status: 'MERGED', number: 77 }} />)
    expect(screen.getByText('status')).toBeInTheDocument()
    expect(screen.getByText('MERGED')).toBeInTheDocument()
    expect(screen.getByText('number')).toBeInTheDocument()
    expect(screen.getByText('77')).toBeInTheDocument()
  })

  it('summarizes nested values instead of inlining them', () => {
    render(<StructuredView data={{ meta: { a: 1 }, tags: [1, 2, 3] }} />)
    expect(screen.getByText('{…}')).toBeInTheDocument()
    expect(screen.getByText('[3]')).toBeInTheDocument()
  })

  it('renders an array of flat objects as a table', () => {
    render(<StructuredView data={[
      { pr: 77, state: 'MERGED' },
      { pr: 76, state: 'OPEN' },
    ]} />)
    // column headers
    expect(screen.getByRole('columnheader', { name: 'pr' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'state' })).toBeInTheDocument()
    // cells
    expect(screen.getByText('MERGED')).toBeInTheDocument()
    expect(screen.getByText('OPEN')).toBeInTheDocument()
  })

  it('always offers a collapsed raw-JSON escape hatch', () => {
    render(<StructuredView data={{ a: 1 }} />)
    const raw = screen.getByText('raw')
    expect(raw).toBeInTheDocument()
    // The raw JSON is present in the DOM (inside the collapsed <details>).
    const details = raw.closest('details') as HTMLElement
    expect(within(details).getByText(/"a": 1/)).toBeInTheDocument()
  })

  it('renders null/boolean values legibly', () => {
    render(<StructuredView data={{ done: true, note: null }} />)
    expect(screen.getByText('true')).toBeInTheDocument()
    expect(screen.getByText('null')).toBeInTheDocument()
  })
})
