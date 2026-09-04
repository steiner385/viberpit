// web/src/ws/pipeline.fuzz.test.ts
//
// Cross-layer fuzz: the full CLIENT rendering pipeline (handleLiveBatch seq
// dedup/heal → transcriptReducer → selectVisibleTurns) driven by a simulated
// server that misbehaves in every way the protocol permits — arbitrary batch
// partitioning, exact duplicate retransmits, silent event loss (a consumed seq
// that is never delivered), heal cycles (replayReset → full replay → replay-
// Complete with the per-attach seq restarting at 0), and stale pre-detach
// frames straddling the reset in one batch (the stale-straddle shape).
//
// The invariants no single-layer test can pin:
//   1. CONVERGENCE — whatever the fault schedule, once the stream is fully
//      delivered (healing where the seq told us to), the rendered transcript
//      EQUALS the one produced by a clean, fault-free delivery of the same
//      ground truth. Loss is recovered by heal, duplicates by upstream dedup,
//      and nothing is applied twice or out of order.
//   2. NO BLANK FLASH — once the view has ever settled non-empty, no point in
//      any heal cycle renders an empty transcript (double-buffer contract).
//
// Server model (deliberately simplified but protocol-faithful): the heal
// replay re-supplies every ground-truth event emitted so far — the disk
// transcript is authoritative and the CLI has written what the daemon emitted.
// The in-progress-turn trim + ring resupply that the real server layers on top
// preserve exactly this net effect (see server/bridge/ws.py _handle_attach).
import { describe, it, expect } from 'vitest'
import fc from 'fast-check'
import { handleLiveBatch, createGapState, type WireMeta } from './liveBatch'
import { transcriptReducer, initialTranscript, type TranscriptState } from '../session/reducer'
import { selectVisibleTurns } from '../session/selectors'
import type { SessionEvent } from '../session/events'

const SID = 's1'

type Fault = 'none' | 'dup' | 'drop'

function buildGroundTruth(turnCount: number, deltasPerTurn: number[]): SessionEvent[] {
  const evs: SessionEvent[] = []
  for (let i = 0; i < turnCount; i++) {
    evs.push({ type: 'user', turnId: `u${i}`, text: `prompt ${i}` } as SessionEvent)
    evs.push({ type: 'turnStart', turnId: `a${i}` } as SessionEvent)
    const deltas = deltasPerTurn[i % deltasPerTurn.length] ?? 1
    for (let d = 0; d < deltas; d++) {
      evs.push({ type: 'textDelta', turnId: `a${i}`, text: `chunk${i}.${d} ` } as SessionEvent)
    }
    evs.push({ type: 'turnEnd', turnId: `a${i}` } as SessionEvent)
  }
  return evs
}

interface Plan {
  turnCount: number
  deltasPerTurn: number[]
  faults: Fault[]        // indexed by ground-truth position (cycled)
  batchSizes: number[]   // live batch partitioning (cycled)
  staleStraddle: boolean // prepend a stale old-epoch frame to each heal batch
}

function simulate(plan: Plan): { visible: ReturnType<typeof selectVisibleTurns>; state: TranscriptState } {
  const truth = buildGroundTruth(plan.turnCount, plan.deltasPerTurn)
  const gap = createGapState()
  let state = initialTranscript
  let everSettledNonEmpty = false

  const applyBatch = (events: SessionEvent[]): boolean => {
    const { dispatch, needsHeal } = handleLiveBatch(events, { activeSessionId: SID, state: gap })
    for (const e of dispatch) state = transcriptReducer(state, e)
    if (!state.replaying && state.turns.length > 0) everSettledNonEmpty = true
    // Invariant 2: the double-buffer never blanks a view that has settled non-empty.
    if (everSettledNonEmpty) expect(selectVisibleTurns(state).length).toBeGreaterThan(0)
    return needsHeal
  }

  // Initial attach: empty-session full replay.
  applyBatch([
    { type: 'replayReset', sessionId: SID } as SessionEvent,
    { type: 'replayComplete', earliestTurn: 0, totalTurns: 0, sessionId: SID } as SessionEvent,
  ])

  let seq = 0     // server's per-attach live seq (restarts at 0 on every heal replay)
  let cursor = 0  // ground-truth index of the next event to emit

  const healCycle = (): boolean => {
    const events: SessionEvent[] = []
    if (plan.staleStraddle) {
      // A stale pre-detach frame flushed by the server FIFO in the SAME batch as the
      // fresh replayReset, stale FIRST (contiguous with the old epoch, so it applies
      // and is then wiped by the reset — the baseline must not be poisoned).
      events.push({ type: 'status', state: 'in_progress', count: 0, sessionId: SID, seq } as SessionEvent)
    }
    events.push({ type: 'replayReset', sessionId: SID } as SessionEvent)
    // Replay frames carry no seq (replay content never affects gap tracking).
    for (let i = 0; i < cursor; i++) events.push({ ...truth[i], sessionId: SID } as SessionEvent)
    events.push({ type: 'replayComplete', earliestTurn: 0, totalTurns: 0, sessionId: SID } as SessionEvent)
    seq = 0
    return applyBatch(events)
  }

  let k = 0
  while (cursor < truth.length) {
    const size = plan.batchSizes[k++ % plan.batchSizes.length] ?? 1
    const batch: SessionEvent[] = []
    while (batch.length < size && cursor < truth.length) {
      // The final event always delivers: an end-of-stream drop is undetectable by seq
      // (nothing follows to reveal the hole) — that tail is owned by reconnect/turn-end
      // resupply on the server, outside this client-pipeline model.
      const fault: Fault = cursor === truth.length - 1
        ? 'none' : (plan.faults[cursor % plan.faults.length] ?? 'none')
      const wire: SessionEvent & WireMeta = { ...truth[cursor], sessionId: SID, seq }
      cursor++
      if (fault === 'drop') { seq++; continue }  // seq consumed, frame never delivered
      seq++
      batch.push(wire)
      if (fault === 'dup') batch.push({ ...wire }) // exact retransmit, same seq
    }
    let needsHeal = batch.length > 0 && applyBatch(batch)
    let guard = 0
    while (needsHeal && guard++ < 4) needsHeal = healCycle()
    expect(needsHeal).toBe(false) // healing must converge, never loop unbounded
  }

  return { visible: selectVisibleTurns(state), state }
}

describe('cross-layer pipeline fuzz (liveBatch → reducer → selectors)', () => {
  const planArb = fc.record({
    turnCount: fc.integer({ min: 1, max: 5 }),
    deltasPerTurn: fc.array(fc.integer({ min: 1, max: 3 }), { minLength: 1, maxLength: 5 }),
    faults: fc.array(fc.constantFrom<Fault>('none', 'dup', 'drop'), { minLength: 1, maxLength: 40 }),
    batchSizes: fc.array(fc.integer({ min: 1, max: 5 }), { minLength: 1, maxLength: 10 }),
    staleStraddle: fc.boolean(),
  })

  it('any batching/duplication/loss schedule with seq-driven healing converges to the ground-truth transcript', () => {
    fc.assert(
      fc.property(planArb, (plan) => {
        const { visible, state } = simulate(plan)
        // Expected: a clean, fault-free delivery of the same ground truth.
        const truth = buildGroundTruth(plan.turnCount, plan.deltasPerTurn)
        let expected = transcriptReducer(initialTranscript, { type: 'replayReset' } as SessionEvent)
        expected = transcriptReducer(expected,
          { type: 'replayComplete', earliestTurn: 0, totalTurns: 0 } as SessionEvent)
        for (const e of truth) expected = transcriptReducer(expected, e)

        expect(state.replaying).toBe(false)
        expect(visible).toEqual(expected.turns)
      }),
      { numRuns: 150 },
    )
  })
})
