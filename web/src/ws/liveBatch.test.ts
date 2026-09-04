// @vitest-environment node
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

  it('passes through untagged lifecycle events for the active session', () => {
    const state = createGapState()
    const replayReset = { type: 'replayReset', sessionId: 's1' } as unknown as SessionEvent
    const r = handleLiveBatch([replayReset], { activeSessionId: 's1', state })
    expect(r.dispatch).toEqual([replayReset])
    expect(r.needsHeal).toBe(false)
  })

  it('drops an exact duplicate frame (seq === prev) silently — no dispatch, no heal, lastSeq unchanged', () => {
    const state = createGapState()
    handleLiveBatch([ev({ seq: 0, sessionId: 's1' })], { activeSessionId: 's1', state })
    const r = handleLiveBatch([ev({ seq: 0, sessionId: 's1', text: 'retransmit' })], {
      activeSessionId: 's1',
      state,
    })
    expect(r.dispatch).toHaveLength(0)
    expect(r.needsHeal).toBe(false)
    expect(state.lastSeq.get('s1')).toBe(0)
  })

  it('drops a stale frame (seq < prev) and flags a heal — the stream is confused', () => {
    const state = createGapState()
    handleLiveBatch([ev({ seq: 5, sessionId: 's1' })], { activeSessionId: 's1', state })
    const r = handleLiveBatch([ev({ seq: 3, sessionId: 's1', text: 'stale' })], {
      activeSessionId: 's1',
      state,
    })
    expect(r.dispatch).toHaveLength(0)
    expect(r.needsHeal).toBe(true)
    expect(state.lastSeq.get('s1')).toBe(5)
  })

  it('still dispatches and heals a forward gap (existing behavior preserved)', () => {
    const state = createGapState()
    handleLiveBatch([ev({ seq: 0, sessionId: 's1' })], { activeSessionId: 's1', state })
    const r = handleLiveBatch([ev({ seq: 2, sessionId: 's1' })], { activeSessionId: 's1', state })
    expect(r.dispatch).toHaveLength(1)
    expect(r.needsHeal).toBe(true)
    expect(state.lastSeq.get('s1')).toBe(2)
  })

  it('applies a replayReset mid-batch in frame order (stale-straddle guard)', () => {
    // The server's single outbound FIFO can flush a stale old-epoch event and the
    // fresh replayReset in the SAME batch, stale FIRST (detach→attach while the
    // batcher held pre-detach frames). Processing must be single-pass in frame
    // order: the stale seq lands, THEN the reset clears the baseline — so the new
    // live epoch's seq 0 is accepted. A pre-scan reset (reset applied before the
    // batch is walked) lets the stale seq re-poison the fresh baseline → seq 0
    // registers a false gap → detach+attach heal loop mid-turn.
    const state = createGapState()
    const r1 = handleLiveBatch(
      [
        ev({ seq: 7, sessionId: 's1', text: 'stale straggler' }),
        { type: 'replayReset', sessionId: 's1' } as unknown as SessionEvent,
        ev({ sessionId: 's1', text: 'replayed history' }),
        { type: 'replayComplete', sessionId: 's1' } as unknown as SessionEvent,
      ],
      { activeSessionId: 's1', state },
    )
    expect(r1.needsHeal).toBe(false)
    const r2 = handleLiveBatch([ev({ seq: 0, sessionId: 's1' })], { activeSessionId: 's1', state })
    expect(r2.needsHeal).toBe(false)
  })

  it('an untagged replayReset re-baselines the active session', () => {
    // Legacy/edge frames may omit the sessionId tag; the reset then applies to
    // the session being viewed (same fallback the client always used).
    const state = createGapState()
    handleLiveBatch([ev({ seq: 5, sessionId: 's1' })], { activeSessionId: 's1', state })
    handleLiveBatch([{ type: 'replayReset' } as unknown as SessionEvent], { activeSessionId: 's1', state })
    const r = handleLiveBatch([ev({ seq: 0, sessionId: 's1' })], { activeSessionId: 's1', state })
    expect(r.needsHeal).toBe(false)
  })

  it('flags a heal when the FIRST post-reset frame arrives above seq 0 (leading loss)', () => {
    // The server restarts its per-attach seq at 0 and a GAP sentinel consumes a seq
    // precisely to leave a detectable hole — but a hole BEFORE the first delivered
    // frame was invisible against a deleted baseline (leading loss swallowed
    // silently; found by the cross-layer pipeline fuzz). replayReset now seeds the
    // baseline to -1 so seq 0 is contiguous and a first frame at seq>0 heals.
    const state = createGapState()
    handleLiveBatch([{ type: 'replayReset', sessionId: 's1' } as unknown as SessionEvent],
      { activeSessionId: 's1', state })
    const r = handleLiveBatch([ev({ seq: 2, sessionId: 's1' })], { activeSessionId: 's1', state })
    expect(r.needsHeal).toBe(true)
    expect(r.dispatch).toHaveLength(1) // the frame itself still renders; the heal back-fills
  })

  // --- srcSeq loss telemetry (audit W2.3): additive, never heals or drops ---

  it('records a srcSeq gap without healing, and still dispatches the frame', () => {
    const state = createGapState()
    handleLiveBatch([ev({ seq: 0, sessionId: 's1', srcSeq: 10 })], { activeSessionId: 's1', state })
    const r = handleLiveBatch([ev({ seq: 1, sessionId: 's1', srcSeq: 12 })], { activeSessionId: 's1', state })
    expect(r.srcSeqGaps).toEqual([{ sessionId: 's1', prev: 10, got: 12 }])
    expect(r.needsHeal).toBe(false)   // srcSeq gap ALONE must not heal
    expect(r.dispatch).toHaveLength(1)   // frame is still delivered
  })

  it('reports no srcSeq gap for contiguous srcSeq', () => {
    const state = createGapState()
    handleLiveBatch([ev({ seq: 0, sessionId: 's1', srcSeq: 10 })], { activeSessionId: 's1', state })
    const r = handleLiveBatch([ev({ seq: 1, sessionId: 's1', srcSeq: 11 })], { activeSessionId: 's1', state })
    expect(r.srcSeqGaps).toHaveLength(0)
  })

  it('resets the srcSeq baseline on replayReset (no false gap after a reconnect replay)', () => {
    const state = createGapState()
    handleLiveBatch([ev({ seq: 0, sessionId: 's1', srcSeq: 99 })], { activeSessionId: 's1', state })
    handleLiveBatch([{ type: 'replayReset', sessionId: 's1' } as unknown as SessionEvent], { activeSessionId: 's1', state })
    const r = handleLiveBatch([ev({ seq: 0, sessionId: 's1', srcSeq: 3 })], { activeSessionId: 's1', state })
    expect(r.srcSeqGaps).toHaveLength(0)   // baseline cleared → the new epoch's srcSeq re-baselines
  })

  it('drops a replayReset tagged for a non-active session (blank-on-navigation guard)', () => {
    // A stale replay for session s2 arriving after the user navigated to s1 must NOT
    // reach s1's reducer: its replayReset would wipe s1's transcript to empty. Now that
    // the server tags replay frames with their sessionId, handleLiveBatch drops it.
    const state = createGapState()
    const r = handleLiveBatch(
      [
        { type: 'replayReset', sessionId: 's2' } as unknown as SessionEvent,
        ev({ sessionId: 's2', text: 'stale history' }),
      ],
      { activeSessionId: 's1', state },
    )
    expect(r.dispatch).toHaveLength(0)
    expect(r.needsHeal).toBe(false)
  })
})
