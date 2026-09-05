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

  // Codex result payloads are single-key status dicts — `{status}` for a file
  // change / MCP / dynamic tool call, `{kind}` for sub-agent activity, `{}` for
  // web search, plan, image view (server/codex/translator.py `_tool_result`).
  // A 1x1 table with a "raw" disclosure for the word "completed" is noise.
  describe('compact tier for small flat payloads', () => {
    it('renders a single-key status payload as one compact line', () => {
      render(<GenericResult payload={{ status: 'completed' }} />)
      const compact = screen.getByTestId('result-compact')
      expect(compact).toHaveTextContent('status')
      expect(compact).toHaveTextContent('completed')
      // No table, and no raw-JSON disclosure for a single word.
      expect(screen.queryByRole('table')).toBeNull()
      expect(screen.queryByText('raw')).toBeNull()
    })

    it('renders a sub-agent activity kind payload compactly', () => {
      render(<GenericResult payload={{ kind: 'completed' }} />)
      expect(screen.getByTestId('result-compact')).toHaveTextContent('kind')
    })

    it('says plainly that a payload-free result carried no details', () => {
      render(<GenericResult payload={{}} />)
      expect(screen.getByTestId('result-compact')).toHaveTextContent(/no details/i)
    })

    it('tints the compact line when the result failed', () => {
      render(<GenericResult ok={false} payload={{ status: 'failed' }} />)
      expect(screen.getByTestId('result-compact').className).toMatch(/red/)
    })

    it('still uses the structured view for anything bigger or nested', () => {
      render(<GenericResult payload={{ a: 1, b: 2, c: 3, d: 4 }} />)
      expect(screen.queryByTestId('result-compact')).toBeNull()
      expect(screen.getByText('raw')).toBeInTheDocument()
    })

    it('still uses the structured view when a value is not a scalar', () => {
      render(<GenericResult payload={{ changes: [{ path: 'a.py' }] }} />)
      expect(screen.queryByTestId('result-compact')).toBeNull()
    })
  })
})
