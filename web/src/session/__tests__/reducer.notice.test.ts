// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from '@/session/reducer'

describe('notice + clear actions', () => {
  it('_notice appends a notice block', () => {
    const s = transcriptReducer(initialTranscript, { type: '_notice', text: 'Model → sonnet' })
    const last = s.turns.at(-1)!
    expect(last.blocks[0]).toMatchObject({ kind: 'notice', text: 'Model → sonnet' })
  })

  it('_clear resets transcript and shows a single notice', () => {
    const seeded = transcriptReducer(initialTranscript, { type: 'user', turnId: 't1', text: 'hi' })
    const s = transcriptReducer(seeded, { type: '_clear', text: 'Conversation cleared' })
    expect(s.turns).toHaveLength(1)
    expect(s.turns[0].blocks[0]).toMatchObject({ kind: 'notice', text: 'Conversation cleared' })
    expect(s.replaying).toBe(false)
  })
})
