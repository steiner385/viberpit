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

  it('renders attached image thumbnails', () => {
    render(<UserBubble text="look" images={[{ id: 'a1', mediaType: 'image/png', filename: 'a.png' }]} />)
    const img = screen.getByAltText('a.png') as HTMLImageElement
    expect(img.getAttribute('src')).toBe('/api/attachments/a1')
  })
})
