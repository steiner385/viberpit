// @vitest-environment node
// web/src/session/reducer.property.test.ts
//
// Property-based invariants for the transcript reducer (audit wave 3):
//
//  1. IDEMPOTENCE of id-keyed events. handleLiveBatch guarantees at-most-once delivery
//     of tagged frames, but a reconnect/heal can legitimately re-deliver the same
//     id-keyed event (turnStart / toolCall / toolResult / permission / question / user).
//     Applying any such batch twice must render identically to applying it once.
//     BOUNDARY: textDelta / thinkingDelta are DELIBERATELY EXCLUDED — they are
//     append-by-design (each delta concatenates), so re-applying them doubles the text.
//     Dedup upstream (liveBatch) is what protects deltas; that layer is modeled off here
//     by simply not generating deltas, matching the reducer's own idempotence contract.
//
//  2. DOUBLE-BUFFER never flashes empty. Starting from a non-empty settled transcript,
//     any sequence of well-formed heal cycles (replayReset → rebuild ≥1 turn →
//     replayComplete, with possible extra mid-cycle resets) keeps selectVisibleTurns
//     non-empty at every intermediate step — the gap-heal never blanks the view.
import { describe, it, expect } from 'vitest'
import fc from 'fast-check'
import { transcriptReducer, initialTranscript, type TranscriptState } from './reducer'
import { selectVisibleTurns } from './selectors'
import type { SessionEvent } from './events'

type Ev = SessionEvent | Parameters<typeof transcriptReducer>[1]
const reduce = (evs: Ev[], initial: TranscriptState = initialTranscript): TranscriptState =>
  evs.reduce((s, e) => transcriptReducer(s, e), initial)

// ── Property 1: idempotence of id-keyed (non-delta) events ─────────────────────

// No textDelta/thinkingDelta (append-by-design — see header). parentToolUseId is omitted:
// this exercises the main-thread reducer path.
//
// BOUNDARY (FINDING F1): the generator honours the two production id-uniqueness invariants
// the reducer's idempotence depends on. Feeding id-collisions that CANNOT occur in
// production yields false non-idempotence, so we model the real contract, not the
// impossible case:
//   (a) user-turn ids and assistant-turn ids are DISJOINT — the backend assigns a user
//       message its own uuid (`u0,u1,…`) and an assistant turn a different uuid (`a0,a2,…`),
//       so a `toolCall.turnId` never equals a `user.turnId` (server tests
//       test_replay_dedup.py / test_export.py). It matters because the `user` case REBUILDS
//       its turn's blocks on upsert — a re-applied `user` would WIPE any tool/permission
//       block sharing that turnId.
//   (b) every toolCall's toolId is GLOBALLY UNIQUE (the SDK guarantees it — see the
//       reducer's own toolResult comment). It matters because toolResult sets the result on
//       EVERY block matching the toolId across all turns; a toolId reused by two toolCalls
//       makes a re-applied toolResult reach a block that did not exist on the first pass.
const userTurnId = fc.constantFrom('u1', 'u2')
const asstTurnId = fc.constantFrom('a1', 'a2', 'a3')

// An abstract op; toolCall toolIds are assigned uniquely (by array index) in the mapping
// below, and toolResult references one of the run's toolCalls (by index modulo count).
type Op =
  | { k: 'user'; t: string }
  | { k: 'turnStart'; t: string }
  | { k: 'toolCall'; t: string }
  | { k: 'toolResult'; ref: number; ok: boolean }
  | { k: 'permission'; t: string; id: string }
  | { k: 'question'; t: string; id: string }

const opArb: fc.Arbitrary<Op> = fc.oneof(
  userTurnId.map((t) => ({ k: 'user', t }) as Op),
  asstTurnId.map((t) => ({ k: 'turnStart', t }) as Op),
  asstTurnId.map((t) => ({ k: 'toolCall', t }) as Op),
  fc.tuple(fc.nat({ max: 30 }), fc.boolean()).map(([ref, ok]) => ({ k: 'toolResult', ref, ok }) as Op),
  fc.tuple(asstTurnId, fc.constantFrom('p1', 'p2')).map(([t, id]) => ({ k: 'permission', t, id }) as Op),
  fc.tuple(asstTurnId, fc.constantFrom('q1', 'q2')).map(([t, id]) => ({ k: 'question', t, id }) as Op),
)

/** Lower an op stream to events, assigning each toolCall a globally-unique toolId and
 *  binding each toolResult to a real toolCall (dropped if the run has no toolCall). */
function opsToEvents(ops: Op[]): Ev[] {
  const toolIds: string[] = []
  ops.forEach((op, i) => { if (op.k === 'toolCall') toolIds.push(`tool-${i}`) })
  const events: Ev[] = []
  ops.forEach((op, i) => {
    switch (op.k) {
      case 'user': events.push({ type: 'user', turnId: op.t, text: `u:${op.t}` } as Ev); break
      case 'turnStart': events.push({ type: 'turnStart', turnId: op.t } as Ev); break
      case 'toolCall': {
        const id = `tool-${i}`
        events.push({ type: 'toolCall', turnId: op.t, toolId: id, name: 'Bash', toolKind: 'bash', input: { cmd: id } } as Ev)
        break
      }
      case 'toolResult': {
        if (toolIds.length === 0) break
        const id = toolIds[op.ref % toolIds.length]
        events.push({ type: 'toolResult', toolId: id, ok: op.ok, payload: { r: id } } as Ev)
        break
      }
      case 'permission': events.push({ type: 'permissionRequest', permId: op.id, turnId: op.t, tool: 'Bash', toolKind: 'bash', input: {}, plan: false } as Ev); break
      case 'question': events.push({ type: 'question', questionId: op.id, turnId: op.t, header: 'h', question: 'why', options: [], multiSelect: false } as Ev); break
    }
  })
  return events
}

describe('transcriptReducer — idempotence of id-keyed events', () => {
  it('applying a tagged-event batch twice renders identically to applying it once', () => {
    fc.assert(
      fc.property(
        fc.array(opArb, { minLength: 1, maxLength: 24 }).map(opsToEvents),
        fc.boolean(),
        (events, replaying) => {
          // `replaying` is held constant across both applications (a single replay pass
          // or a single live pass), matching how the reducer sees a batch.
          const seed: TranscriptState = { ...initialTranscript, replaying }
          const once = reduce(events, seed)
          const twice = reduce([...events, ...events], seed)
          // Rendered content is identical (id-keyed events are upserts, not appends).
          expect(twice.turns).toStrictEqual(once.turns)
          expect(twice.orphanToolResults).toStrictEqual(once.orphanToolResults)
        },
      ),
      { numRuns: 500 },
    )
  })
})

// ── Property 2: double-buffer never flashes empty across a heal ────────────────

/** A non-empty settled transcript to heal from. */
function settledNonEmpty(): TranscriptState {
  return reduce([
    { type: 'turnStart', turnId: 'seed' },
    { type: 'textDelta', turnId: 'seed', text: 'original content' },
    { type: 'turnEnd', turnId: 'seed' },
    { type: 'replayComplete' },
  ])
}

// One well-formed heal cycle: replayReset (+ an optional SECOND back-to-back reset,
// exercising the "second reset mid-replay preserves stale, does not blank" path), then
// a non-empty rebuild, then replayComplete. Both resets precede the rebuild so the cycle
// always COMPLETES on ≥1 rebuilt turn — a heal that legitimately rebuilds to empty is a
// correct (non-buffered) empty view, not the flash-of-blank this invariant guards.
const healCycleArb = fc.record({
  rebuildTurns: fc.array(fc.constantFrom('r1', 'r2', 'r3'), { minLength: 1, maxLength: 3 }),
  extraReset: fc.boolean(),
})

function cycleToEvents(c: { rebuildTurns: string[]; extraReset: boolean }): Ev[] {
  const evs: Ev[] = [{ type: 'replayReset' } as Ev]
  if (c.extraReset) evs.push({ type: 'replayReset' } as Ev)
  c.rebuildTurns.forEach((t) => {
    evs.push({ type: 'turnStart', turnId: t } as Ev)
    evs.push({ type: 'textDelta', turnId: t, text: `rebuilt ${t}` } as Ev)
  })
  evs.push({ type: 'replayComplete' } as Ev)
  return evs
}

describe('transcriptReducer — double-buffer never flashes empty during heal', () => {
  it('selectVisibleTurns stays non-empty at every step across arbitrary heal cycles', () => {
    fc.assert(
      fc.property(fc.array(healCycleArb, { minLength: 1, maxLength: 5 }), (cycles) => {
        let state = settledNonEmpty()
        expect(selectVisibleTurns(state).length).toBeGreaterThan(0)
        for (const cycle of cycles) {
          for (const ev of cycleToEvents(cycle)) {
            state = transcriptReducer(state, ev)
            // The invariant: the user never sees a blank transcript mid-heal.
            expect(selectVisibleTurns(state).length).toBeGreaterThan(0)
          }
          // After a completed cycle the rebuilt live content is what's visible.
          expect(selectVisibleTurns(state)).toBe(state.turns)
          expect(state.replaying).toBe(false)
        }
      }),
      { numRuns: 300 },
    )
  })

  it('right after replayReset the visible view is the captured pre-reset content', () => {
    fc.assert(
      fc.property(fc.constant(null), () => {
        const before = settledNonEmpty()
        const reset = transcriptReducer(before, { type: 'replayReset' } as Ev)
        expect(reset.turns).toHaveLength(0) // live cleared…
        expect(selectVisibleTurns(reset)).toBe(before.turns) // …but stale served
        expect(selectVisibleTurns(reset).length).toBeGreaterThan(0)
      }),
      { numRuns: 1 },
    )
  })
})
