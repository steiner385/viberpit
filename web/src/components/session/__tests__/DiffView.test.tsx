import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DiffView } from '../DiffView'

describe('DiffView', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('renders old_string lines with - prefix', () => {
    render(<DiffView input={{ old_string: 'old line', new_string: 'new line' }} />)
    expect(screen.getByText('- old line')).toBeInTheDocument()
  })

  it('renders new_string lines with + prefix', () => {
    render(<DiffView input={{ old_string: 'old', new_string: 'new' }} />)
    expect(screen.getByText('+ new')).toBeInTheDocument()
  })

  it('renders write content (content only) as added lines', () => {
    render(<DiffView input={{ content: 'added content' }} />)
    expect(screen.getByText('+ added content')).toBeInTheDocument()
  })

  it('caps at 200 lines and shows "show all" button', () => {
    const manyLines = Array.from({ length: 300 }, (_, i) => `line${i}`).join('\n')
    render(<DiffView input={{ content: manyLines }} />)
    expect(screen.getByRole('button', { name: /show all/i })).toBeInTheDocument()
  })

  it('"show all" button reveals hidden lines', async () => {
    const user = userEvent.setup()
    const manyLines = Array.from({ length: 300 }, (_, i) => `line${i}`).join('\n')
    render(<DiffView input={{ content: manyLines }} />)
    await user.click(screen.getByRole('button', { name: /show all/i }))
    expect(screen.queryByRole('button', { name: /show all/i })).toBeNull()
  })

  it('applies error styling when the edit failed (ok=false)', () => {
    const { container } = render(<DiffView input={{ old_string: 'a', new_string: 'b' }} ok={false} />)
    expect(container.firstChild).toHaveClass('border-red-400')
  })

  it('has no error border by default', () => {
    const { container } = render(<DiffView input={{ old_string: 'a', new_string: 'b' }} />)
    expect(container.firstChild).not.toHaveClass('border-red-400')
  })
})

describe('DiffView: a Codex file change', () => {
  // The app-server reports WHICH files changed and how, never the text either
  // side. `buildLines` therefore returns nothing, and rendering its empty result
  // was an empty bordered box that claimed a diff existed.
  const changes = [
    { path: 'server/app.py', kind: 'update' },
    { path: 'server/new.py', kind: 'add' },
    { path: 'server/old.py', kind: 'delete' },
  ]

  it('lists every changed path instead of an empty diff', () => {
    render(<DiffView input={{ changes }} />)
    expect(screen.getByText('server/app.py')).toBeInTheDocument()
    expect(screen.getByText('server/new.py')).toBeInTheDocument()
    expect(screen.getByText('server/old.py')).toBeInTheDocument()
  })

  it('names what happened to each file', () => {
    render(<DiffView input={{ changes }} />)
    expect(screen.getByText('updated')).toBeInTheDocument()
    expect(screen.getByText('added')).toBeInTheDocument()
    expect(screen.getByText('deleted')).toBeInTheDocument()
  })

  it('still renders a real diff when one is reconstructable', () => {
    // The Claude path must be untouched by the fallback.
    render(<DiffView input={{ old_string: 'old', new_string: 'new' }} />)
    expect(screen.getByText('- old')).toBeInTheDocument()
  })

  it('renders nothing rather than a change list when there is no diff at all', () => {
    const { container } = render(<DiffView input={{}} />)
    expect(container.querySelector('ul')).toBeNull()
  })
})
