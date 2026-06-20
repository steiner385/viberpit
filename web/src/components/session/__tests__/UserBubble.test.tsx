import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { UserBubble } from '../UserBubble'

describe('UserBubble', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('renders the text', () => {
    render(<UserBubble text="Hello world" />)
    expect(screen.getByText('Hello world')).toBeInTheDocument()
  })

  it('preserves whitespace', () => {
    render(<UserBubble text={"line1\nline2"} />)
    const el = screen.getByText(/line1/)
    expect(el).toBeInTheDocument()
  })
})
