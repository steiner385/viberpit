import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { BashResult } from '../BashResult'

describe('BashResult', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('renders content field', () => {
    render(<BashResult payload={{ content: 'command output' }} />)
    expect(screen.getByText(/command output/)).toBeInTheDocument()
  })

  it('renders stdout + stderr if no content field', () => {
    render(<BashResult payload={{ stdout: 'out here', stderr: 'err here' }} />)
    expect(screen.getByText(/out here/)).toBeInTheDocument()
    expect(screen.getByText(/err here/)).toBeInTheDocument()
  })

  it('strips ANSI escape codes', () => {
    render(<BashResult payload={{ content: '\x1b[31mred\x1b[0m' }} />)
    expect(screen.getByText(/red/)).toBeInTheDocument()
  })

  it('renders list-shaped content as text, not raw JSON', () => {
    // A tool_result whose content is a list of blocks (common from the SDK).
    render(<BashResult payload={{ content: [{ type: 'text', text: 'block output' }] }} />)
    expect(screen.getByText(/block output/)).toBeInTheDocument()
    expect(screen.queryByText(/"type"/)).toBeNull()
  })

  it('renders script tag content as literal text (no execution)', () => {
    const xss = '<script>window.__XSS_TEST__ = true</script>'
    render(<BashResult payload={{ content: xss }} />)
    // The text must appear as text content
    expect(screen.getByText(xss)).toBeInTheDocument()
    // No <script> element created in the DOM
    expect(document.querySelectorAll('script').length).toBe(0)
    // The XSS flag was not set
    expect((window as Record<string, unknown>).__XSS_TEST__).toBeUndefined()
  })

  it('applies error styling when ok is false', () => {
    const { container } = render(<BashResult payload={{ content: 'error' }} ok={false} />)
    expect(container.firstChild).toHaveClass('bg-red-50')
  })
})
