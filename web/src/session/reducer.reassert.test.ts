// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from './reducer'
import type { SessionEvent } from './events'

describe('transcriptReducer — _trackSentPrompt buffer', () => {
  it('adds a pending entry with a stable id and bumps the seq', () => {
    const s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'hello' })
    expect(s.pendingSent).toEqual([{ id: 'pending:0', text: 'hello' }])
    expect(s.pendingSentSeq).toBe(1)
    // tracking must not itself create a turn — the live echo renders it
    expect(s.turns).toHaveLength(0)
  })

  it('keeps insertion order and unique ids across multiple sends', () => {
    let s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'a' })
    s = transcriptReducer(s, { type: '_trackSentPrompt', text: 'b' })
    expect(s.pendingSent).toEqual([
      { id: 'pending:0', text: 'a' },
      { id: 'pending:1', text: 'b' },
    ])
    expect(s.pendingSentSeq).toBe(2)
  })

  it('caps the buffer at 20 entries, dropping the oldest', () => {
    let s = initialTranscript
    for (let i = 0; i < 25; i++) {
      s = transcriptReducer(s, { type: '_trackSentPrompt', text: `p${i}` })
    }
    expect(s.pendingSent).toHaveLength(20)
    expect(s.pendingSent[0]).toEqual({ id: 'pending:5', text: 'p5' })
    expect(s.pendingSent[19]).toEqual({ id: 'pending:24', text: 'p24' })
    expect(s.pendingSentSeq).toBe(25)
  })
})

const RESET: SessionEvent = { type: 'replayReset' }
const DONE: SessionEvent = { type: 'replayComplete' }
const userEv = (turnId: string, text: string): SessionEvent => ({ type: 'user', turnId, text })

function reduce(start: ReturnType<typeof transcriptReducer>, evs: (SessionEvent | { type: '_trackSentPrompt'; text: string })[]) {
  return evs.reduce(transcriptReducer, start)
}

function userTexts(s: ReturnType<typeof transcriptReducer>) {
  return s.turns.filter((t) => t.role === 'user').map((t) => {
    const b = t.blocks.find((bl) => bl.kind === 'text')
    return b && b.kind === 'text' ? b.text : ''
  })
}

describe('transcriptReducer — re-assert on replay', () => {
  it('a live echo (replaying=false) does NOT consume the buffer', () => {
    let s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'hello' })
    // initialTranscript.replaying is true; close replay first so we are live
    s = transcriptReducer(s, DONE)            // replaying -> false
    s = transcriptReducer(s, userEv('live-1', 'hello'))  // live echo
    expect(s.pendingSent).toHaveLength(1)     // still buffered (not durable yet)
    expect(userTexts(s)).toEqual(['hello'])
  })

  it('a disk-replay user event (replaying=true) consumes the matching entry', () => {
    let s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'hello' })
    s = transcriptReducer(s, DONE)
    s = reduce(s, [RESET, userEv('disk-1', 'hello'), DONE])  // RESET sets replaying=true
    expect(s.pendingSent).toHaveLength(0)
    expect(userTexts(s)).toEqual(['hello'])   // exactly one, from the replay
  })

  it('race repro: replayReset+replayComplete with no matching replay re-asserts the prompt', () => {
    let s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'hello' })
    s = transcriptReducer(s, DONE)
    s = transcriptReducer(s, userEv('live-1', 'hello'))  // shown live
    // reconnect: wipe, replay brings back NOTHING (disk hasn't flushed), complete
    s = reduce(s, [RESET, DONE])
    expect(userTexts(s)).toEqual(['hello'])   // re-asserted, not lost
    expect(s.turns[s.turns.length - 1].id).toBe('pending:0')
    expect(s.pendingSent).toHaveLength(1)     // still unconfirmed on disk
  })

  it('heals on the next cycle once disk has the prompt (no duplicate)', () => {
    let s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'hello' })
    s = transcriptReducer(s, DONE)
    s = reduce(s, [RESET, DONE])              // re-asserted (race)
    s = reduce(s, [RESET, userEv('disk-1', 'hello'), DONE])  // disk caught up
    expect(userTexts(s)).toEqual(['hello'])   // exactly one
    expect(s.pendingSent).toHaveLength(0)
  })

  it('no-wipe attach: replayComplete with a live echo present does not duplicate', () => {
    let s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'hello' })
    s = transcriptReducer(s, DONE)
    s = transcriptReducer(s, userEv('live-1', 'hello'))  // live echo, turn present
    // empty-transcript attach: server sends replayComplete with NO replayReset
    s = transcriptReducer(s, DONE)
    expect(userTexts(s)).toEqual(['hello'])   // still exactly one
  })

  it('FIFO duplicates: two identical sends, one on disk, one re-asserted', () => {
    let s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'yes' })
    s = transcriptReducer(s, { type: '_trackSentPrompt', text: 'yes' })
    s = transcriptReducer(s, DONE)
    s = reduce(s, [RESET, userEv('disk-1', 'yes'), DONE])  // only one 'yes' on disk
    expect(userTexts(s)).toEqual(['yes', 'yes'])  // one replayed + one re-asserted
    expect(s.pendingSent).toHaveLength(1)         // one still unconfirmed
  })

  it('replayReset preserves the buffer', () => {
    let s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'keep' })
    s = transcriptReducer(s, RESET)
    expect(s.pendingSent).toEqual([{ id: 'pending:0', text: 'keep' }])
    expect(s.pendingSentSeq).toBe(1)
  })

  it('a live echo ADOPTS a re-asserted pending turn instead of duplicating it', () => {
    // Mid-turn heal: the disk replay trimmed the open turn, so replayComplete
    // re-asserted the prompt as pending:0. The open-turn resupply then re-emits
    // the real user echo LIVE — it must re-key the pending turn in place, not
    // append a second bubble with the same text.
    let s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'hello' })
    s = transcriptReducer(s, DONE)
    s = reduce(s, [RESET, DONE])              // re-asserted as pending:0
    expect(s.turns[s.turns.length - 1].id).toBe('pending:0')
    s = transcriptReducer(s, userEv('real-1', 'hello'))   // live resupply echo
    expect(userTexts(s)).toEqual(['hello'])   // exactly one bubble
    expect(s.turns[s.turns.length - 1].id).toBe('real-1') // re-keyed in place
    expect(s.pendingSent).toHaveLength(1)     // durability buffer untouched (live ≠ durable)
  })

  it('adoption matches by text — a different live prompt still appends', () => {
    let s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'hello' })
    s = transcriptReducer(s, DONE)
    s = reduce(s, [RESET, DONE])              // pending:0 'hello' re-asserted
    s = transcriptReducer(s, userEv('real-2', 'other'))
    expect(userTexts(s)).toEqual(['hello', 'other'])
  })

  it('a disk-replay echo also adopts a lingering pending turn (no duplicate)', () => {
    // pending:0 was re-asserted, then a NON-wiping replay (no replayReset) brings
    // the prompt from disk: adopt the pending turn AND consume the buffer.
    let s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'hello' })
    s = transcriptReducer(s, DONE)
    s = reduce(s, [RESET, DONE])              // pending:0 re-asserted
    s = reduce(s, [RESET, DONE])              // wiped + re-asserted again (still pending:0)
    s = transcriptReducer(s, { type: 'replayReset' })
    s = transcriptReducer(s, userEv('disk-9', 'hello'))   // replaying=true
    s = transcriptReducer(s, DONE)
    expect(userTexts(s)).toEqual(['hello'])
    expect(s.pendingSent).toHaveLength(0)
  })

  it('does not re-assert an older durable prompt once a newer one is confirmed on disk', () => {
    // The user-reported symptom: an OLD prompt reappears (untimestamped) at the
    // very bottom after a reconnect. 'old' was confirmed by a LIVE echo only (so
    // the buffer never drained it) and has since scrolled out of the replay's
    // tail window. A newer 'recent' prompt IS in the window. On reconnect the
    // replay re-emits 'recent', proving it is on disk — and FIFO write order means
    // 'old' (sent earlier) is necessarily on disk too. So 'old' must be dropped,
    // not resurrected below newer content.
    let s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'old' })    // pending:0
    s = transcriptReducer(s, { type: '_trackSentPrompt', text: 'recent' })                      // pending:1
    s = transcriptReducer(s, DONE)                              // go live
    s = transcriptReducer(s, userEv('live-old', 'old'))         // live echo — buffer NOT drained
    s = transcriptReducer(s, userEv('live-recent', 'recent'))
    // reconnect: wipe; replay window returns only the newer prompt ('old' scrolled out)
    s = reduce(s, [RESET, userEv('disk-recent', 'recent'), DONE])
    expect(userTexts(s)).toEqual(['recent'])   // 'old' NOT resurrected at the bottom
    expect(s.pendingSent).toHaveLength(0)      // both proven durable via FIFO order
  })

  it('initial replayComplete (no reset) does not re-assert even with a populated buffer', () => {
    // A prompt is tracked but no replayReset has occurred (hadReset stays false);
    // the initial/empty-transcript replayComplete must not synthesize a turn before
    // the live echo arrives.
    let s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'hello' })
    s = transcriptReducer(s, DONE)        // initial replayComplete, hadReset=false
    expect(userTexts(s)).toEqual([])      // nothing re-asserted
    expect(s.pendingSent).toHaveLength(1) // still buffered
  })
})
