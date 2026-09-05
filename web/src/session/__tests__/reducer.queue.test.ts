// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from '@/session/reducer'

describe('queueUpdate reducer', () => {
  it('stores structured queued items with images', () => {
    const s = transcriptReducer(initialTranscript, {
      type: 'queueUpdate',
      items: [
        { text: 'first', images: [] },
        { text: 'see this', images: [{ url: '/api/attachments/a', mime: 'image/png', name: 's.png' }] },
      ],
    })
    expect(s.queued).toHaveLength(2)
    expect(s.queued[1].text).toBe('see this')
    expect(s.queued[1].images[0].url).toBe('/api/attachments/a')
  })

  it('starts empty', () => {
    expect(initialTranscript.queued).toEqual([])
  })
})
