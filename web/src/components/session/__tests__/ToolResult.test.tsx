import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ToolResult } from '../ToolResult'

describe('ToolResult', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('dispatches edit → DiffView, reconstructing the diff from input (not the result)', () => {
    render(
      <ToolResult
        kind="edit"
        ok={true}
        input={{ old_string: 'old', new_string: 'new' }}
        payload={{ content: 'The file has been updated.' }}
      />,
    )
    expect(screen.getByText('- old')).toBeInTheDocument()
    expect(screen.getByText('+ new')).toBeInTheDocument()
    // The result success message must NOT be rendered as a diff line.
    expect(screen.queryByText(/has been updated/)).toBeNull()
  })

  it('dispatches write → DiffView', () => {
    render(<ToolResult kind="write" ok={true} input={{ content: 'written' }} payload={{ content: 'ok' }} />)
    expect(screen.getByText('+ written')).toBeInTheDocument()
  })

  it('dispatches read → FileContent', () => {
    render(<ToolResult kind="read" ok={true} input={{}} payload={{ content: 'file data' }} />)
    expect(screen.getByText(/file data/)).toBeInTheDocument()
  })

  it('dispatches bash → BashResult', () => {
    render(<ToolResult kind="bash" ok={true} input={{}} payload={{ content: 'bash output' }} />)
    expect(screen.getByText(/bash output/)).toBeInTheDocument()
  })

  it('dispatches todo → TodoList', () => {
    render(<ToolResult kind="todo" ok={true} input={{}} payload={{ todos: [{ content: 'task', status: 'pending' }] }} />)
    expect(screen.getByText('task')).toBeInTheDocument()
  })

  it('dispatches search → SearchResult (lines + count, no envelope)', () => {
    render(<ToolResult kind="search" ok={true} name="Grep" input={{}} payload={{ content: 'a.ts:1: x\nb.ts:2: x' }} />)
    expect(screen.getByText('2 matches')).toBeInTheDocument()
    expect(screen.queryByText(/"content"/)).toBeNull()
  })

  it('dispatches task → SubagentResult (markdown, no envelope)', () => {
    render(<ToolResult kind="task" ok={true} input={{}} payload={{ content: 'done **well**' }} />)
    expect(screen.getByText('well').tagName).toBe('STRONG')
  })

  it('dispatches web → WebResult (shows url, no envelope)', () => {
    render(<ToolResult kind="web" ok={true} name="WebFetch" input={{ url: 'https://x.io' }} payload={{ content: 'body' }} />)
    expect(screen.getByText('https://x.io')).toBeInTheDocument()
    expect(screen.queryByText(/"content"/)).toBeNull()
  })

  it('dispatches mcp → GenericResult, extracting text not the envelope', () => {
    render(<ToolResult kind="mcp" ok={true} name="mcp__github__list_prs" input={{}} payload={{ content: 'hello' }} />)
    expect(screen.getByText('hello')).toBeInTheDocument()
    expect(screen.queryByText(/"content"/)).toBeNull()
  })

  it('renders an unknown kind structurally, never as the raw envelope', () => {
    render(<ToolResult kind="unknown" ok={true} input={{}} payload={{ foo: 'bar' }} />)
    expect(screen.getByText('foo')).toBeInTheDocument()
    expect(screen.getByText('bar')).toBeInTheDocument()
  })

  it('styles text results as an error when ok is false', () => {
    const { container } = render(<ToolResult kind="unknown" ok={false} input={{}} payload={{ content: 'boom' }} />)
    expect(container.querySelector('pre')).toHaveClass('bg-red-50')
  })

  it('passes ok through to read → FileContent', () => {
    const { container } = render(<ToolResult kind="read" ok={false} input={{}} payload={{ content: 'denied' }} />)
    expect(container.querySelector('pre')).toHaveClass('bg-red-50')
  })

  it('passes ok through to edit → DiffView', () => {
    const { container } = render(
      <ToolResult kind="edit" ok={false} input={{ old_string: 'a', new_string: 'b' }} payload={{ content: 'failed' }} />,
    )
    expect(container.firstChild).toHaveClass('border-red-400')
  })
})
