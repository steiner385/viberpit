import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { FileContent } from '../FileContent'

describe('FileContent', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('renders file content', () => {
    render(<FileContent payload={{ content: 'file content here' }} />)
    expect(screen.getByText(/file content here/)).toBeInTheDocument()
  })

  it('strips ANSI escape codes', () => {
    render(<FileContent payload={{ content: '\x1b[32mgreen\x1b[0m' }} />)
    expect(screen.getByText(/green/)).toBeInTheDocument()
    expect(screen.queryByText(/\x1b/)).toBeNull()
  })

  it('shows "show more" when content exceeds 300 lines', () => {
    const big = Array.from({ length: 400 }, (_, i) => `line${i}`).join('\n')
    render(<FileContent payload={{ content: big }} />)
    expect(screen.getByRole('button', { name: /show.*more/i })).toBeInTheDocument()
  })

  it('clicking "show more" reveals all lines', async () => {
    const user = userEvent.setup()
    const big = Array.from({ length: 400 }, (_, i) => `line${i}`).join('\n')
    render(<FileContent payload={{ content: big }} />)
    await user.click(screen.getByRole('button', { name: /show.*more/i }))
    expect(screen.queryByRole('button', { name: /show.*more/i })).toBeNull()
  })

  it('falls back to JSON when content field missing', () => {
    render(<FileContent payload={{ other: 'data' }} />)
    expect(screen.getByText(/other/)).toBeInTheDocument()
  })
})
