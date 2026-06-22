import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { GenericResult } from '../GenericResult'

describe('GenericResult', () => {
  it('renders string content as text, not as the raw envelope', () => {
    render(<GenericResult payload={{ content: 'src/foo.ts\nsrc/bar.ts' }} />)
    expect(screen.getByText(/src\/foo\.ts/)).toBeInTheDocument()
    // The leak we are fixing: the literal envelope key must NOT be shown.
    expect(screen.queryByText(/"content"/)).toBeNull()
  })

  it('joins a text-block-array content into text', () => {
    render(<GenericResult payload={{ content: [{ type: 'text', text: 'hello from mcp' }] }} />)
    expect(screen.getByText(/hello from mcp/)).toBeInTheDocument()
  })

  it('renders structured array content as a table', () => {
    render(<GenericResult payload={{ content: [{ pr: 77, state: 'MERGED' }] }} />)
    expect(screen.getByRole('columnheader', { name: 'pr' })).toBeInTheDocument()
    expect(screen.getByText('MERGED')).toBeInTheDocument()
  })

  it('renders an object payload (no content key) as key/value rows', () => {
    render(<GenericResult payload={{ foo: 'bar' }} />)
    expect(screen.getByText('foo')).toBeInTheDocument()
    expect(screen.getByText('bar')).toBeInTheDocument()
  })

  it('strips ANSI escapes from text content', () => {
    render(<GenericResult payload={{ content: '\x1b[31mred\x1b[0m text' }} />)
    expect(screen.getByText('red text')).toBeInTheDocument()
  })

  it('applies error styling to text content when ok is false', () => {
    const { container } = render(<GenericResult ok={false} payload={{ content: 'boom' }} />)
    expect(container.querySelector('pre')).toHaveClass('bg-red-50')
  })
})
