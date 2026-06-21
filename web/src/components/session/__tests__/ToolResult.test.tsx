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

  it('renders JSON pre for unknown kind', () => {
    render(<ToolResult kind="unknown" ok={true} input={{}} payload={{ foo: 'bar' }} />)
    expect(screen.getByText(/foo/)).toBeInTheDocument()
  })

  it('renders JSON pre for search kind', () => {
    render(<ToolResult kind="search" ok={true} input={{}} payload={{ results: [] }} />)
    expect(screen.getByText(/results/)).toBeInTheDocument()
  })
})
