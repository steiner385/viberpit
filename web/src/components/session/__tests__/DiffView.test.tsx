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
