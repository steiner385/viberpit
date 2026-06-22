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

  it('renders attached image thumbnails using the wire url', () => {
    render(<UserBubble images={[{ url: '/api/attachments/a1', mime: 'image/png', name: 'a.png' }]} />)
    const img = screen.getByAltText('a.png') as HTMLImageElement
    expect(img.getAttribute('src')).toBe('/api/attachments/a1')
  })

  it('renders a replayed data-url image (no attachment id available)', () => {
    render(<UserBubble images={[{ url: 'data:image/png;base64,AAAA', mime: 'image/png', name: '' }]} />)
    const img = document.querySelector('img') as HTMLImageElement
    expect(img.getAttribute('src')).toBe('data:image/png;base64,AAAA')
  })
})
