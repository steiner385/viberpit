// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript, OPTIMISTIC_USER_ID } from './reducer'

describe('transcriptReducer — optimistic user prompt', () => {
  it('_seedUserPrompt appends one optimistic user turn', () => {
    const s = transcriptReducer(initialTranscript, { type: '_seedUserPrompt', text: 'hello world' })
    expect(s.turns).toHaveLength(1)
    expect(s.turns[0]).toMatchObject({ id: OPTIMISTIC_USER_ID, role: 'user' })
    expect(s.turns[0].blocks[0]).toMatchObject({ kind: 'text', text: 'hello world' })
  })

  it('_seedUserPrompt is idempotent (no duplicate on double-dispatch)', () => {
    const s1 = transcriptReducer(initialTranscript, { type: '_seedUserPrompt', text: 'hi' })
    const s2 = transcriptReducer(s1, { type: '_seedUserPrompt', text: 'hi' })
    expect(s2.turns).toHaveLength(1)
    expect(s2).toBe(s1) // unchanged reference
  })

  it('a matching user echo adopts the optimistic turn in place (no duplicate)', () => {
    const seeded = transcriptReducer(initialTranscript, { type: '_seedUserPrompt', text: 'do the thing' })
    const adopted = transcriptReducer(seeded, { type: 'user', turnId: 'real-1', text: 'do the thing' })
    expect(adopted.turns).toHaveLength(1)
    expect(adopted.turns[0].id).toBe('real-1')
    expect(adopted.turns[0].blocks[0]).toMatchObject({ id: 'real-1:u', text: 'do the thing' })
  })

  it('a user echo with different text still adopts the single optimistic turn (fallback)', () => {
    const seeded = transcriptReducer(initialTranscript, { type: '_seedUserPrompt', text: 'typed text' })
    const adopted = transcriptReducer(seeded, { type: 'user', turnId: 'real-2', text: 'normalized text' })
    expect(adopted.turns).toHaveLength(1)
    expect(adopted.turns[0].id).toBe('real-2')
    expect(adopted.turns[0].blocks[0]).toMatchObject({ text: 'normalized text' })
  })

  it('without a seed, a user event appends normally (unchanged behavior)', () => {
    const s = transcriptReducer(initialTranscript, { type: 'user', turnId: 't1', text: 'plain' })
    expect(s.turns).toHaveLength(1)
    expect(s.turns[0].id).toBe('t1')
  })

  it('a second user event after adoption appends a new turn', () => {
    let s = transcriptReducer(initialTranscript, { type: '_seedUserPrompt', text: 'first' })
    s = transcriptReducer(s, { type: 'user', turnId: 'real-1', text: 'first' })
    s = transcriptReducer(s, { type: 'user', turnId: 'real-2', text: 'second' })
    expect(s.turns.map((t) => t.id)).toEqual(['real-1', 'real-2'])
  })
})
