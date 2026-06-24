import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from './reducer'
import type { SessionEvent } from './events'

describe('condensing reducer', () => {
  it('stores expectedMs on active, clears on inactive', () => {
    const s1 = transcriptReducer(initialTranscript, { type: 'condensing', active: true, expectedMs: 8000 } as SessionEvent)
    expect(s1.condense).toEqual({ active: true, expectedMs: 8000 })
    const s2 = transcriptReducer(s1, { type: 'condensing', active: false } as SessionEvent)
    expect(s2.condense).toBeNull()
  })
  it('defaults expectedMs to 12000 when omitted', () => {
    const s = transcriptReducer(initialTranscript, { type: 'condensing', active: true } as SessionEvent)
    expect(s.condense).toEqual({ active: true, expectedMs: 12000 })
  })
})
