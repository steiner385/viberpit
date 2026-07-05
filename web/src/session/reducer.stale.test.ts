/**
 * Double-buffer during replay: a gap-heal (replayReset → replay → replayComplete)
 * must not blank the visible transcript. On replayReset the currently rendered
 * content is captured into stale* state and served while `replaying` is true; on
 * replayComplete the freshly rebuilt live content atomically becomes visible and
 * stale is cleared. See reducer.ts (replayReset / replayComplete) and
 * selectors.ts (selectVisibleTurns / selectVisibleSubAgents).
 */

import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from './reducer'
import type { TranscriptState } from './reducer'
import type { SessionEvent } from './events'
import { selectVisibleTurns, selectVisibleSubAgents } from './selectors'

function reduce(events: (SessionEvent | Parameters<typeof transcriptReducer>[1])[], initial = initialTranscript): TranscriptState {
  return events.reduce((s, ev) => transcriptReducer(s, ev as Parameters<typeof transcriptReducer>[1]), initial)
}

/** A settled single-turn transcript (replaying=false), as it would exist before a heal. */
function settled(): TranscriptState {
  return reduce([
    { type: 'turnStart', turnId: 't1' },
    { type: 'textDelta', turnId: 't1', text: 'hello world' },
    { type: 'turnEnd', turnId: 't1' },
    { type: 'replayComplete' },
  ])
}

describe('transcript double-buffer during replay', () => {
  it('captures the rendered transcript into stale on replayReset and serves it while replaying', () => {
    const before = settled()
    expect(before.replaying).toBe(false)
    expect(before.turns).toHaveLength(1)

    const reset = transcriptReducer(before, { type: 'replayReset' } as SessionEvent)

    // Live arrays are cleared exactly as before, and replaying is re-entered.
    expect(reset.turns).toHaveLength(0)
    expect(reset.replaying).toBe(true)
    // Stale captured the pre-reset content (same reference — no copy).
    expect(reset.staleTurns).toBe(before.turns)

    // The VISIBLE transcript is unchanged — the stale content is served.
    const visible = selectVisibleTurns(reset)
    expect(visible).toBe(before.turns)
    expect(visible).toHaveLength(1)
    // The empty-state placeholder condition (turns.length === 0) is therefore false.
    expect(visible.length === 0).toBe(false)
  })

  it('accumulates replay events invisibly, then swaps to the new replay on replayComplete', () => {
    const before = settled()
    const reset = transcriptReducer(before, { type: 'replayReset' } as SessionEvent)

    // Replay rebuilds the transcript (here: a second turn appears on disk). The
    // live arrays grow, but the visible view stays on the stale content.
    const mid = reduce([
      { type: 'turnStart', turnId: 't1' },
      { type: 'textDelta', turnId: 't1', text: 'hello world' },
      { type: 'turnStart', turnId: 't2' },
      { type: 'textDelta', turnId: 't2', text: 'a follow-up' },
    ], reset)
    expect(mid.turns).toHaveLength(2)          // live rebuilt
    expect(selectVisibleTurns(mid)).toBe(before.turns) // still showing stale
    expect(selectVisibleTurns(mid)).toHaveLength(1)

    const done = transcriptReducer(mid, { type: 'replayComplete' } as SessionEvent)
    expect(done.replaying).toBe(false)
    // Atomic swap: the freshly rebuilt live content is now visible.
    expect(selectVisibleTurns(done)).toBe(done.turns)
    expect(selectVisibleTurns(done)).toHaveLength(2)
    // Stale is cleared so it can never leak into a later render.
    expect(done.staleTurns).toHaveLength(0)
  })

  it('first attach (empty transcript) → replayReset leaves stale empty, behavior identical to today', () => {
    // initialTranscript is empty and already replaying=true.
    const reset = transcriptReducer(initialTranscript, { type: 'replayReset' } as SessionEvent)
    expect(reset.staleTurns).toHaveLength(0)
    expect(reset.turns).toHaveLength(0)
    // Visible falls through to the (empty) live turns — placeholder shows, as today.
    expect(selectVisibleTurns(reset)).toBe(reset.turns)
    expect(selectVisibleTurns(reset)).toHaveLength(0)
  })

  it('a second replayReset mid-replay does NOT overwrite stale with the half-built replay', () => {
    const before = settled()
    const reset1 = transcriptReducer(before, { type: 'replayReset' } as SessionEvent)

    // A partial replay lands (turn rebuilt but replayComplete never arrives)...
    const partial = reduce([
      { type: 'turnStart', turnId: 't1' },
      { type: 'textDelta', turnId: 't1', text: 'half' },
    ], reset1)
    expect(partial.turns).toHaveLength(1)
    expect(partial.replaying).toBe(true)

    // ...then a second replayReset (another heal) arrives before completion. Stale
    // must remain the ORIGINAL good content, not the half-built partial replay.
    const reset2 = transcriptReducer(partial, { type: 'replayReset' } as SessionEvent)
    expect(reset2.staleTurns).toBe(before.turns)
    expect(selectVisibleTurns(reset2)).toBe(before.turns)
    expect(selectVisibleTurns(reset2)).toHaveLength(1)
  })

  it('captures and serves sub-agent threads across a heal', () => {
    const before = reduce([
      { type: 'toolCall', turnId: 't1', toolId: 'task-1', name: 'Task', toolKind: 'unknown', input: { subagent_type: 'Explore' } },
      { type: 'textDelta', turnId: 's1', text: 'sub thinking', parentToolUseId: 'task-1' },
      { type: 'replayComplete' },
    ])
    expect(before.subAgents['task-1']).toBeDefined()

    const reset = transcriptReducer(before, { type: 'replayReset' } as SessionEvent)
    expect(reset.subAgents).toEqual({})              // live sub-threads cleared
    expect(reset.staleSubAgents).toBe(before.subAgents)
    expect(selectVisibleSubAgents(reset)).toBe(before.subAgents)

    const done = transcriptReducer(reset, { type: 'replayComplete' } as SessionEvent)
    expect(done.staleSubAgents).toEqual({})          // stale cleared on swap
    expect(selectVisibleSubAgents(done)).toBe(done.subAgents)
  })
})
