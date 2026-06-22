import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SearchResult } from '../SearchResult'

describe('SearchResult', () => {
  it('renders matched lines and a count summary for Grep', () => {
    render(<SearchResult name="Grep" payload={{ content: 'src/a.ts:1: foo\nsrc/b.ts:2: foo' }} />)
    expect(screen.getByText(/src\/a\.ts:1: foo/)).toBeInTheDocument()
    expect(screen.getByText('2 matches')).toBeInTheDocument()
  })

  it('summarizes Glob/LS results as files', () => {
    render(<SearchResult name="Glob" payload={{ content: 'a.ts\nb.ts\nc.ts' }} />)
    expect(screen.getByText('3 files')).toBeInTheDocument()
  })

  it('shows an empty-state summary when there are no results', () => {
    render(<SearchResult name="Grep" payload={{ content: '' }} />)
    expect(screen.getByText('no matches')).toBeInTheDocument()
  })

  it('never leaks the raw envelope JSON', () => {
    render(<SearchResult name="Grep" payload={{ content: 'hit' }} />)
    expect(screen.queryByText(/"content"/)).toBeNull()
  })
})
