// web/src/ws/liveBatch.test.ts
import { describe, it, expect } from 'vitest'
import { createGapState, resetSeq, handleLiveBatch } from './liveBatch'
import type { SessionEvent } from '@/session/events'

const ev = (over: Record<string, unknown>): SessionEvent =>
  ({ type: 'textDelta', turnId: 't1', text: 'x', ...over }) as unknown as SessionEvent

describe('handleLiveBatch', () => {
  it('dispatches contiguous events for the active session with no heal', () => {
    const state = createGapState()
    const r = handleLiveBatch(
      [ev({ seq: 0, sessionId: 's1' }), ev({ seq: 1, sessionId: 's1' })],
      { activeSessionId: 's1', state },
    )
    expect(r.dispatch).toHaveLength(2)
    expect(r.needsHeal).toBe(false)
  })

  it('flags a heal when a seq is missing', () => {
    const state = createGapState()
    handleLiveBatch([ev({ seq: 0, sessionId: 's1' })], { activeSessionId: 's1', state })
    const r = handleLiveBatch([ev({ seq: 2, sessionId: 's1' })], { activeSessionId: 's1', state })
    expect(r.needsHeal).toBe(true)
  })

  it('drops events belonging to a non-active session', () => {
    const state = createGapState()
    const r = handleLiveBatch(
      [ev({ seq: 0, sessionId: 's2' })],
      { activeSessionId: 's1', state },
    )
    expect(r.dispatch).toHaveLength(0)
    expect(r.needsHeal).toBe(false)   // another session's gap must not heal ours
  })

  it('re-baselines after resetSeq (reconnect replay) without a false gap', () => {
    const state = createGapState()
    handleLiveBatch([ev({ seq: 5, sessionId: 's1' })], { activeSessionId: 's1', state })
    resetSeq(state, 's1')
    const r = handleLiveBatch([ev({ seq: 0, sessionId: 's1' })], { activeSessionId: 's1', state })
    expect(r.needsHeal).toBe(false)
  })

  it('passes through events without seq/sessionId (replay/lifecycle) untouched', () => {
    const state = createGapState()
    const replayReset = { type: 'replayReset' } as SessionEvent
    const r = handleLiveBatch([replayReset], { activeSessionId: 's1', state })
    expect(r.dispatch).toEqual([replayReset])
    expect(r.needsHeal).toBe(false)
  })
})
