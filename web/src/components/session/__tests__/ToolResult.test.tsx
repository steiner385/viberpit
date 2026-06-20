import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ToolResult } from '../ToolResult'

describe('ToolResult', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('dispatches edit → DiffView', () => {
    render(<ToolResult kind="edit" ok={true} payload={{ old_string: 'old', new_string: 'new' }} />)
    expect(screen.getByText('- old')).toBeInTheDocument()
  })

  it('dispatches write → DiffView', () => {
    render(<ToolResult kind="write" ok={true} payload={{ content: 'written' }} />)
    expect(screen.getByText('+ written')).toBeInTheDocument()
  })

  it('dispatches read → FileContent', () => {
    render(<ToolResult kind="read" ok={true} payload={{ content: 'file data' }} />)
    expect(screen.getByText(/file data/)).toBeInTheDocument()
  })

  it('dispatches bash → BashResult', () => {
    render(<ToolResult kind="bash" ok={true} payload={{ content: 'bash output' }} />)
    expect(screen.getByText(/bash output/)).toBeInTheDocument()
  })

  it('dispatches todo → TodoList', () => {
    render(<ToolResult kind="todo" ok={true} payload={{ todos: [{ content: 'task', status: 'pending' }] }} />)
    expect(screen.getByText('task')).toBeInTheDocument()
  })

  it('renders JSON pre for unknown kind', () => {
    render(<ToolResult kind="unknown" ok={true} payload={{ foo: 'bar' }} />)
    expect(screen.getByText(/foo/)).toBeInTheDocument()
  })

  it('renders JSON pre for search kind', () => {
    render(<ToolResult kind="search" ok={true} payload={{ results: [] }} />)
    expect(screen.getByText(/results/)).toBeInTheDocument()
  })
})
