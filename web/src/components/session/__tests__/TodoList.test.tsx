import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { TodoList } from '../TodoList'

describe('TodoList', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('renders todos with status markers', () => {
    render(<TodoList payload={{
      todos: [
        { content: 'Done task', status: 'completed' },
        { content: 'Active task', status: 'in_progress' },
        { content: 'Pending task', status: 'pending' },
      ]
    }} />)
    expect(screen.getByText('Done task')).toBeInTheDocument()
    expect(screen.getByText('Active task')).toBeInTheDocument()
    expect(screen.getByText('Pending task')).toBeInTheDocument()
    expect(screen.getByText('✓')).toBeInTheDocument()
    expect(screen.getByText('◐')).toBeInTheDocument()
    expect(screen.getByText('○')).toBeInTheDocument()
  })

  it('falls back to JSON pre when todos is not an array', () => {
    render(<TodoList payload={{ other: 'stuff' }} />)
    expect(screen.getByText(/other/)).toBeInTheDocument()
  })
})
