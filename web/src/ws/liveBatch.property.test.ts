// @vitest-environment node
// web/src/ws/liveBatch.property.test.ts
//
// Property-based invariants for handleLiveBatch (audit wave 3). The example-based
// suite in liveBatch.test.ts covers hand-picked cases; these drive the dedup/heal
// contract with fast-check over ARBITRARY frame sequences — dups, stales, forward
// gaps, interleaved sessions, and replayReset re-baselines in any order.
//
// Two layers of assertion:
//   1. Independent structural invariants on the OUTPUT alone (no reimplementation of
//      the algorithm): dispatch is an order-preserving subsequence of the input, no
//      foreign-session frame leaks, and active-session dispatched seqs are strictly
//      increasing within each replayReset-delimited epoch (⇒ no duplicate/stale is
//      ever dispatched).
//   2. Model-based exactness: a reference implementation of the documented contract
//      reproduces { dispatch, needsHeal } for every call. Divergence would be a real
//      bug (recorded as a FINDING), not a test to soften.
import { describe, it, expect } from 'vitest'
import fc from 'fast-check'
import { createGapState, handleLiveBatch, type GapState } from './liveBatch'
import type { SessionEvent } from '@/session/events'

const ACTIVE = 'active'

// A generated frame: either a seq-bearing content event or a replayReset, optionally
// tagged with a sessionId (active, a foreign session, or untagged/legacy).
type GenFrame =
  | { kind: 'event'; sid?: string; seq?: number }
  | { kind: 'reset'; sid?: string }

const sidArb = fc.constantFrom<string | undefined>(ACTIVE, 'other', undefined)

const frameArb: fc.Arbitrary<GenFrame> = fc.oneof(
  fc.record({
    kind: fc.constant<'event'>('event'),
    sid: sidArb,
    seq: fc.oneof(fc.integer({ min: 0, max: 8 }), fc.constant(undefined)),
  }),
  fc.record({ kind: fc.constant<'reset'>('reset'), sid: sidArb }),
)

// A run = a list of BATCHES (each handleLiveBatch call gets one batch); gap state
// persists across calls, so multi-batch runs exercise cross-call baselines.
const runArb = fc.array(fc.array(frameArb, { maxLength: 6 }), { maxLength: 6 })

function toEvent(f: GenFrame): SessionEvent {
  if (f.kind === 'reset') {
    return { type: 'replayReset', ...(f.sid ? { sessionId: f.sid } : {}) } as unknown as SessionEvent
  }
  return {
    type: 'textDelta', turnId: 't', text: 'x',
    ...(f.sid ? { sessionId: f.sid } : {}),
    ...(f.seq !== undefined ? { seq: f.seq } : {}),
  } as unknown as SessionEvent
}

// ── Reference model of the documented contract (model-based oracle) ────────────
function refBatch(last: Map<string, number>, batch: GenFrame[]) {
  const dispatch: GenFrame[] = []
  let needsHeal = false
  for (const f of batch) {
    if (f.kind === 'reset') {
      const sid = f.sid ?? ACTIVE
      // Seed -1, not delete: the server restarts its per-attach seq at 0, so the first
      // post-reset frame must be seq 0 — a first frame above 0 is leading loss (a GAP
      // sentinel consumed the seq) and must heal. See resetSeq in liveBatch.ts.
      last.set(sid, -1)
    }
    if (f.sid && f.sid !== ACTIVE) continue // foreign session → dropped
    if (f.kind === 'event' && typeof f.seq === 'number' && f.sid) {
      const prev = last.get(f.sid)
      if (prev !== undefined && f.seq <= prev) {
        if (f.seq < prev) needsHeal = true // regression heals; exact dup is silent
        continue
      }
      if (prev !== undefined && f.seq !== prev + 1) needsHeal = true // forward gap
      last.set(f.sid, f.seq)
    }
    dispatch.push(f)
  }
  return { dispatch, needsHeal }
}

describe('handleLiveBatch — property invariants', () => {
  it('output is always an order-preserving subsequence of the input with no foreign leak', () => {
    fc.assert(
      fc.property(runArb, (run) => {
        const state = createGapState()
        for (const batch of run) {
          const events = batch.map(toEvent)
          const { dispatch } = handleLiveBatch(events, { activeSessionId: ACTIVE, state })
          // Subsequence + order preservation: walk input, matching dispatched refs in order.
          let di = 0
          for (const e of events) {
            if (di < dispatch.length && dispatch[di] === e) di++
          }
          expect(di).toBe(dispatch.length)
          // No frame explicitly owned by a foreign session survives.
          for (const e of dispatch) {
            const sid = (e as { sessionId?: string }).sessionId
            expect(sid === undefined || sid === ACTIVE).toBe(true)
          }
        }
      }),
      { numRuns: 400 },
    )
  })

  it('active-session dispatched seqs strictly increase within each replayReset epoch (no dup/stale dispatched)', () => {
    fc.assert(
      fc.property(runArb, (run) => {
        const state = createGapState()
        // Fold the whole run's dispatch into one ordered stream; reset epochs on any
        // dispatched active/untagged replayReset (the only resets that reach dispatch).
        let last = -Infinity
        for (const batch of run) {
          const { dispatch } = handleLiveBatch(batch.map(toEvent), { activeSessionId: ACTIVE, state })
          for (const e of dispatch) {
            const t = (e as { type?: string }).type
            const sid = (e as { sessionId?: string }).sessionId
            const seq = (e as { seq?: number }).seq
            if (t === 'replayReset') { last = -Infinity; continue }
            if (typeof seq === 'number' && sid === ACTIVE) {
              expect(seq).toBeGreaterThan(last) // strictly increasing ⇒ no dup, no stale
              last = seq
            }
          }
        }
      }),
      { numRuns: 400 },
    )
  })

  it('matches the reference contract for dispatch and needsHeal on every call (model-based)', () => {
    fc.assert(
      fc.property(runArb, (run) => {
        const state: GapState = createGapState()
        const refLast = new Map<string, number>()
        for (const batch of run) {
          const events = batch.map(toEvent)
          const actual = handleLiveBatch(events, { activeSessionId: ACTIVE, state })
          const expected = refBatch(refLast, batch)
          expect(actual.needsHeal).toBe(expected.needsHeal)
          // Compare dispatch by structural shape (indices align 1:1 with the model).
          expect(actual.dispatch.length).toBe(expected.dispatch.length)
          actual.dispatch.forEach((e, i) => {
            const ef = expected.dispatch[i]
            expect((e as { seq?: number }).seq).toBe(ef.kind === 'event' ? ef.seq : undefined)
            expect((e as { type?: string }).type).toBe(ef.kind === 'reset' ? 'replayReset' : 'textDelta')
          })
        }
      }),
      { numRuns: 500 },
    )
  })
})
