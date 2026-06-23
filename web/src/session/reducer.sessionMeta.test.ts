import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from './reducer'

describe('sessionMeta updates model', () => {
  it('sets model from sessionMeta event', () => {
    const s = transcriptReducer(initialTranscript, {
      type: 'sessionMeta', model: 'opus', sessionId: 's1',
    })
    expect(s.model).toBe('opus')
  })
})
