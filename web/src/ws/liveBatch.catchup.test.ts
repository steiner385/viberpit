// @vitest-environment node
// web/src/ws/liveBatch.catchup.test.ts
// Wave-3 cursor catch-up (audit W2.3) — the srcContiguous gate + resumeOk handling in the
// WS-client layer. catchUpCursor(state, sid) returns the srcSeq to send as `sinceSrcSeq`
// ONLY when the source stream has stayed contiguous since the last replayComplete/resumeOk.
import { describe, it, expect } from 'vitest'
import { createGapState, handleLiveBatch, catchUpCursor, catchUpEpoch, invalidateCatchUp } from './liveBatch'
import type { SessionEvent } from '@/session/events'

const ev = (over: Record<string, unknown>): SessionEvent =>
  ({ type: 'textDelta', turnId: 't1', text: 'x', ...over }) as unknown as SessionEvent
const frame = (t: string, over: Record<string, unknown> = {}): SessionEvent =>
  ({ type: t, ...over }) as unknown as SessionEvent

describe('catchUpCursor / srcContiguous', () => {
  it('is null before any baseline is established (no replayComplete yet)', () => {
    const state = createGapState()
    handleLiveBatch([ev({ seq: 0, sessionId: 's1', srcSeq: 5 })], { activeSessionId: 's1', state })
    // No replayComplete → not contiguous → no cursor even though a srcSeq was seen.
    expect(catchUpCursor(state, 's1')).toBeNull()
  })

  it('returns lastSrcSeq once replayComplete establishes contiguity and srcSeqs are unbroken', () => {
    const state = createGapState()
    handleLiveBatch([frame('replayComplete', { sessionId: 's1' })], { activeSessionId: 's1', state })
    handleLiveBatch(
      [ev({ seq: 0, sessionId: 's1', srcSeq: 10 }), ev({ seq: 1, sessionId: 's1', srcSeq: 11 })],
      { activeSessionId: 's1', state },
    )
    expect(catchUpCursor(state, 's1')).toBe(11)
  })

  it('goes null after a srcSeq hole breaks contiguity (loss → must full-replay)', () => {
    const state = createGapState()
    handleLiveBatch([frame('replayComplete', { sessionId: 's1' })], { activeSessionId: 's1', state })
    handleLiveBatch([ev({ seq: 0, sessionId: 's1', srcSeq: 10 })], { activeSessionId: 's1', state })
    // Per-attach seq stays contiguous (1) but srcSeq jumps 10→12 (a real daemon→edge loss).
    const r = handleLiveBatch([ev({ seq: 1, sessionId: 's1', srcSeq: 12 })], { activeSessionId: 's1', state })
    expect(r.srcSeqGaps).toHaveLength(1)
    expect(catchUpCursor(state, 's1')).toBeNull()
  })

  it('replayReset resets contiguity (a full replay is starting)', () => {
    const state = createGapState()
    handleLiveBatch([frame('replayComplete', { sessionId: 's1' })], { activeSessionId: 's1', state })
    handleLiveBatch([ev({ seq: 0, sessionId: 's1', srcSeq: 10 })], { activeSessionId: 's1', state })
    expect(catchUpCursor(state, 's1')).toBe(10)
    handleLiveBatch([frame('replayReset', { sessionId: 's1' })], { activeSessionId: 's1', state })
    expect(catchUpCursor(state, 's1')).toBeNull()
  })
})

describe('invalidateCatchUp (fresh user-initiated attach)', () => {
  it('forces catchUpCursor → null by clearing lastSrcSeq + srcContiguous', () => {
    const state = createGapState()
    handleLiveBatch([frame('replayComplete', { sessionId: 's1' })], { activeSessionId: 's1', state })
    handleLiveBatch([ev({ seq: 0, sessionId: 's1', srcSeq: 10 })], { activeSessionId: 's1', state })
    expect(catchUpCursor(state, 's1')).toBe(10)
    invalidateCatchUp(state, 's1')
    expect(catchUpCursor(state, 's1')).toBeNull()
    expect(state.lastSrcSeq.has('s1')).toBe(false)
    expect(state.srcContiguous.has('s1')).toBe(false)
  })

  it('is per-session: invalidating s1 leaves s2 cursor intact', () => {
    const state = createGapState()
    for (const sid of ['s1', 's2']) {
      handleLiveBatch([frame('replayComplete', { sessionId: sid })], { activeSessionId: sid, state })
      handleLiveBatch([ev({ seq: 0, sessionId: sid, srcSeq: 20 })], { activeSessionId: sid, state })
    }
    invalidateCatchUp(state, 's1')
    expect(catchUpCursor(state, 's1')).toBeNull()
    expect(catchUpCursor(state, 's2')).toBe(20)
  })
})

describe('resumeOk handling', () => {
  it('is NOT dispatched to the reducer (transcript continues untouched)', () => {
    const state = createGapState()
    const r = handleLiveBatch(
      [frame('resumeOk', { sessionId: 's1' }), ev({ seq: 0, sessionId: 's1', srcSeq: 4 })],
      { activeSessionId: 's1', state },
    )
    // Only the tail textDelta is dispatched; resumeOk is consumed by the transport layer.
    expect(r.dispatch.map((e) => (e as { type: string }).type)).toEqual(['textDelta'])
  })

  it('resets the per-attach seq baseline so the ring tail (restarting at seq 0) is accepted', () => {
    const state = createGapState()
    // Establish a high per-attach seq baseline.
    handleLiveBatch([ev({ seq: 9, sessionId: 's1', srcSeq: 9 })], { activeSessionId: 's1', state })
    // resumeOk then the ring tail restarts per-attach seq at 0 — must NOT look like a gap.
    const r = handleLiveBatch(
      [frame('resumeOk', { sessionId: 's1' }), ev({ seq: 0, sessionId: 's1', srcSeq: 10 })],
      { activeSessionId: 's1', state },
    )
    expect(r.needsHeal).toBe(false)
    expect(r.dispatch).toHaveLength(1) // the tail event
  })

  it('marks the source contiguous again so a subsequent heal can re-catch-up', () => {
    const state = createGapState()
    // A prior hole broke contiguity.
    handleLiveBatch([frame('replayComplete', { sessionId: 's1' })], { activeSessionId: 's1', state })
    handleLiveBatch([ev({ seq: 0, sessionId: 's1', srcSeq: 10 })], { activeSessionId: 's1', state })
    handleLiveBatch([ev({ seq: 1, sessionId: 's1', srcSeq: 12 })], { activeSessionId: 's1', state })
    expect(catchUpCursor(state, 's1')).toBeNull()
    // resumeOk re-establishes a clean baseline; the next tail event sets the cursor.
    handleLiveBatch(
      [frame('resumeOk', { sessionId: 's1' }), ev({ seq: 0, sessionId: 's1', srcSeq: 13 })],
      { activeSessionId: 's1', state },
    )
    expect(catchUpCursor(state, 's1')).toBe(13)
  })
})

describe('catchUpEpoch / srcEpoch (#497)', () => {
  it('is null before any baseline carries an epoch', () => {
    const state = createGapState()
    handleLiveBatch([frame('replayComplete', { sessionId: 's1' })], { activeSessionId: 's1', state })
    expect(catchUpEpoch(state, 's1')).toBeNull()
  })

  it('replayComplete records its epoch for the session', () => {
    const state = createGapState()
    handleLiveBatch([frame('replayComplete', { sessionId: 's1', epoch: 'E1' })], { activeSessionId: 's1', state })
    expect(catchUpEpoch(state, 's1')).toBe('E1')
  })

  it('resumeOk records its epoch (re-anchored after the reset)', () => {
    const state = createGapState()
    handleLiveBatch(
      [frame('resumeOk', { sessionId: 's1', epoch: 'E2' }), ev({ seq: 0, sessionId: 's1', srcSeq: 5 })],
      { activeSessionId: 's1', state },
    )
    expect(catchUpEpoch(state, 's1')).toBe('E2')
    expect(catchUpCursor(state, 's1')).toBe(5)
  })

  it('replayComplete WITHOUT an epoch clears any stale epoch (local mode / old server)', () => {
    const state = createGapState()
    handleLiveBatch([frame('replayComplete', { sessionId: 's1', epoch: 'E1' })], { activeSessionId: 's1', state })
    expect(catchUpEpoch(state, 's1')).toBe('E1')
    handleLiveBatch([frame('replayComplete', { sessionId: 's1' })], { activeSessionId: 's1', state })
    expect(catchUpEpoch(state, 's1')).toBeNull()
  })

  it('invalidateCatchUp clears the remembered epoch too', () => {
    const state = createGapState()
    handleLiveBatch([frame('replayComplete', { sessionId: 's1', epoch: 'E1' })], { activeSessionId: 's1', state })
    handleLiveBatch([ev({ seq: 0, sessionId: 's1', srcSeq: 5 })], { activeSessionId: 's1', state })
    invalidateCatchUp(state, 's1')
    expect(catchUpEpoch(state, 's1')).toBeNull()
    expect(state.srcEpoch.has('s1')).toBe(false)
  })

  it('replayReset clears the remembered epoch (a full replay is starting)', () => {
    const state = createGapState()
    handleLiveBatch([frame('replayComplete', { sessionId: 's1', epoch: 'E1' })], { activeSessionId: 's1', state })
    expect(catchUpEpoch(state, 's1')).toBe('E1')
    handleLiveBatch([frame('replayReset', { sessionId: 's1' })], { activeSessionId: 's1', state })
    expect(catchUpEpoch(state, 's1')).toBeNull()
  })
})
