// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from './reducer'

/**
 * `origin` has to reach the Turn through EVERY user-turn path, not just the plain
 * append. A nudge that arrives while an optimistic or pending turn is outstanding
 * (which is exactly when autopilot acts — the session is mid-flight) would otherwise
 * be adopted into that turn and lose its provenance, rendering as something the
 * operator typed. That adoption path is the one this file exists to pin.
 */
describe('transcriptReducer — prompt origin', () => {
  it('carries origin onto an appended user turn', () => {
    const s = transcriptReducer(initialTranscript, {
      type: 'user', turnId: 'u1', text: 'nudge', origin: { kind: 'autopilot' },
    })
    expect(s.turns[0].origin).toEqual({ kind: 'autopilot' })
  })

  it('leaves origin absent for a typed prompt', () => {
    const s = transcriptReducer(initialTranscript, { type: 'user', turnId: 'u1', text: 'ship it' })
    expect(s.turns[0].origin).toBeUndefined()
  })

  it('keeps origin when the echo adopts an outstanding pending turn', () => {
    const pending = transcriptReducer(initialTranscript, {
      type: '_trackSentPrompt', text: 'nudge',
    })
    const s = transcriptReducer(pending, {
      type: 'user', turnId: 'u1', text: 'nudge', origin: { kind: 'autopilot' },
    })
    const users = s.turns.filter((t) => t.role === 'user')
    expect(users).toHaveLength(1)
    expect(users[0].origin).toEqual({ kind: 'autopilot' })
  })

  it('an upsert of the same turn can correct the origin', () => {
    const first = transcriptReducer(initialTranscript, {
      type: 'user', turnId: 'u1', text: 'nudge', origin: { kind: 'autopilot' },
    })
    const s = transcriptReducer(first, { type: 'user', turnId: 'u1', text: 'nudge' })
    expect(s.turns[0].origin).toBeUndefined()
  })

  it('carries the label and the inferred flag through unchanged', () => {
    const s = transcriptReducer(initialTranscript, {
      type: 'user', turnId: 'u1', text: 'rebase',
      origin: { kind: 'automation', label: 'PR Doctor', inferred: true },
    })
    expect(s.turns[0].origin).toEqual({ kind: 'automation', label: 'PR Doctor', inferred: true })
  })
})
