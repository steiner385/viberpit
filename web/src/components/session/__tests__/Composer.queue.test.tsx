import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { Composer } from '../Composer'

// Composer looks up the session's engine (for attachment gating) via the
// sessions list cache; no test here passes a sessionId that resolves, so an
// empty list keeps `engine` undefined (unrestricted) for every case.
vi.mock('@/api/hooks', () => ({ useSessions: () => ({ data: [] }) }))

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
