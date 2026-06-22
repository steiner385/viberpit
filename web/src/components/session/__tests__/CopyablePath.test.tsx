import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { CopyablePath } from '../CopyablePath'

describe('CopyablePath', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
    })
  })

  it('renders the path text inside a code element', () => {
    render(<CopyablePath text="web/src/foo.tsx:60" />)
    const el = screen.getByText('web/src/foo.tsx:60')
    expect(el.tagName).toBe('CODE')
    expect(el).toHaveAttribute('role', 'button')
    expect(el).toHaveAttribute('title', 'Copy path')
  })

  it('copies the text and shows feedback on click', async () => {
    render(<CopyablePath text="web/src/foo.tsx:60" />)
    fireEvent.click(screen.getByText('web/src/foo.tsx:60'))
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('web/src/foo.tsx:60')
    await waitFor(() =>
      expect(screen.getByText('web/src/foo.tsx:60')).toHaveAttribute('title', 'Copied!'),
    )
  })

  it('copies on Enter key', async () => {
    render(<CopyablePath text="foo.py" />)
    fireEvent.keyDown(screen.getByText('foo.py'), { key: 'Enter' })
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('foo.py')
  })

  it('does not throw when the clipboard write rejects', async () => {
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
    })
    render(<CopyablePath text="foo.py" />)
    expect(() => fireEvent.click(screen.getByText('foo.py'))).not.toThrow()
  })
})
