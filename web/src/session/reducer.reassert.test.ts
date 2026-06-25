import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from './reducer'

describe('transcriptReducer — _trackSentPrompt buffer', () => {
  it('adds a pending entry with a stable id and bumps the seq', () => {
    const s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'hello' })
    expect(s.pendingSent).toEqual([{ id: 'pending:0', text: 'hello' }])
    expect(s.pendingSentSeq).toBe(1)
    // tracking must not itself create a turn — the live echo renders it
    expect(s.turns).toHaveLength(0)
  })

  it('keeps insertion order and unique ids across multiple sends', () => {
    let s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'a' })
    s = transcriptReducer(s, { type: '_trackSentPrompt', text: 'b' })
    expect(s.pendingSent).toEqual([
      { id: 'pending:0', text: 'a' },
      { id: 'pending:1', text: 'b' },
    ])
    expect(s.pendingSentSeq).toBe(2)
  })

  it('caps the buffer at 20 entries, dropping the oldest', () => {
    let s = initialTranscript
    for (let i = 0; i < 25; i++) {
      s = transcriptReducer(s, { type: '_trackSentPrompt', text: `p${i}` })
    }
    expect(s.pendingSent).toHaveLength(20)
    expect(s.pendingSent[0]).toEqual({ id: 'pending:5', text: 'p5' })
    expect(s.pendingSent[19]).toEqual({ id: 'pending:24', text: 'p24' })
    expect(s.pendingSentSeq).toBe(25)
  })
})
