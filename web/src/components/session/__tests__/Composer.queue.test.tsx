import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { Composer } from '../Composer'

describe('Composer queued strip', () => {
  it('renders queued item text and image thumbnails', () => {
    render(<Composer
      status={{ state: 'working', count: 1 }}
      onPrompt={() => {}} onInterrupt={() => {}} onRequestSuggestions={() => Promise.resolve([])}
      queued={[{ text: 'analyze this', images: [{ url: '/api/attachments/a', mime: 'image/png', name: 's.png' }] }]}
    />)
    expect(screen.getByText('analyze this')).toBeTruthy()
    const img = screen.getByAltText('s.png') as HTMLImageElement
    expect(img.src).toContain('/api/attachments/a')
  })
})
