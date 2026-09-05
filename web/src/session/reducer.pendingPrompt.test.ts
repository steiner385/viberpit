// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from './reducer'

/**
 * A composer send must be VISIBLE immediately — the whole point of the pending
 * bubble. `_trackSentPrompt` used to only fill the `pendingSent` re-assert buffer,
 * which renders nothing, so the transcript sat unchanged until the SDK echoed the
 * prompt back (30 s–2 min on a session the server has to resume first).
 */
describe('transcriptReducer — pending prompt bubble', () => {
  it('_trackSentPrompt appends a visible user turn for the sent prompt', () => {
    const s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'do the thing' })
    expect(s.turns).toHaveLength(1)
    expect(s.turns[0]).toMatchObject({ id: 'pending:0', role: 'user' })
    expect(s.turns[0].blocks[0]).toMatchObject({ kind: 'text', text: 'do the thing' })
  })

  it('the pending turn carries the "sent" stage by default', () => {
    const s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'hi' })
    expect(s.turns[0].pending).toBe('sent')
  })

  it('a prompt buffered by a closed socket is staged "buffered", not "sent"', () => {
    const s = transcriptReducer(initialTranscript, {
      type: '_trackSentPrompt', text: 'hi', stage: 'buffered',
    })
    expect(s.turns[0].pending).toBe('buffered')
  })

  it('still fills the pendingSent re-assert buffer (unchanged contract)', () => {
    const s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'hi' })
    expect(s.pendingSent).toEqual([{ id: 'pending:0', text: 'hi' }])
    expect(s.pendingSentSeq).toBe(1)
  })

  it('the real user echo adopts the pending turn in place (no duplicate bubble)', () => {
    const sent = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'do the thing' })
    const echoed = transcriptReducer(sent, { type: 'user', turnId: 'real-1', text: 'do the thing' })
    expect(echoed.turns).toHaveLength(1)
    expect(echoed.turns[0].id).toBe('real-1')
    expect(echoed.turns[0].pending).toBeUndefined()
  })

  it('two sends produce two distinct pending turns in send order', () => {
    let s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'first' })
    s = transcriptReducer(s, { type: '_trackSentPrompt', text: 'second' })
    expect(s.turns.map((t) => t.id)).toEqual(['pending:0', 'pending:1'])
  })

  it('replayComplete does not re-append a pending turn already on screen', () => {
    // A prompt sent DURING a replay (after a reset) is both in the buffer and already
    // rendered. Without a presence check the re-assert would duplicate the bubble.
    let s = transcriptReducer(initialTranscript, { type: 'replayReset' })
    s = transcriptReducer(s, { type: '_trackSentPrompt', text: 'sent mid-replay' })
    s = transcriptReducer(s, { type: 'replayComplete' })
    expect(s.turns.filter((t) => t.id.startsWith('pending:'))).toHaveLength(1)
  })

  it('replayComplete still re-asserts a buffered prompt whose turn a reset wiped', () => {
    let s = transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'racing the write' })
    s = transcriptReducer(s, { type: 'replayReset' })
    s = transcriptReducer(s, { type: 'replayComplete' })
    expect(s.turns).toHaveLength(1)
    expect(s.turns[0].id).toBe('pending:0')
  })
})

describe('transcriptReducer — pending prompt stage transitions', () => {
  const sent = () => transcriptReducer(initialTranscript, { type: '_trackSentPrompt', text: 'hello' })

  it('_promptStage advances the oldest un-acked pending turn', () => {
    const s = transcriptReducer(sent(), { type: '_promptStage', stage: 'received' })
    expect(s.turns[0].pending).toBe('received')
  })

  it('_promptStage advances only the oldest un-acked turn, not every pending turn', () => {
    let s = sent()
    s = transcriptReducer(s, { type: '_trackSentPrompt', text: 'second' })
    s = transcriptReducer(s, { type: '_promptStage', stage: 'received' })
    expect(s.turns.map((t) => t.pending)).toEqual(['received', 'sent'])
  })

  it('_promptStage never walks a stage backwards', () => {
    let s = transcriptReducer(sent(), { type: '_promptStage', stage: 'delivered' })
    s = transcriptReducer(s, { type: '_promptStage', stage: 'received' })
    expect(s.turns[0].pending).toBe('delivered')
  })

  it('_promptStage is a no-op with no pending turns', () => {
    const s = transcriptReducer(initialTranscript, { type: '_promptStage', stage: 'received' })
    expect(s).toBe(initialTranscript)
  })

  it('a server-refused prompt goes back to "buffered" and STAYS in the re-assert buffer', () => {
    // promptRejected re-queues the prompt in the socket's hold buffer (client.ts), so
    // it is not lost — it retries on the next flush. Dropping it from pendingSent, or
    // showing it as failed, would both be lies.
    const s = transcriptReducer(sent(), { type: '_promptHeld', text: 'hello' })
    expect(s.turns[0].pending).toBe('buffered')
    expect(s.pendingSent).toEqual([{ id: 'pending:0', text: 'hello' }])
  })

  it('_promptHeld moves a stage BACKWARDS (the one path that may)', () => {
    let s = transcriptReducer(sent(), { type: '_promptStage', stage: 'delivered' })
    s = transcriptReducer(s, { type: '_promptHeld', text: 'hello' })
    expect(s.turns[0].pending).toBe('buffered')
  })

  it('_promptHeld only affects the matching text', () => {
    let s = sent()
    s = transcriptReducer(s, { type: '_trackSentPrompt', text: 'second' })
    s = transcriptReducer(s, { type: '_promptHeld', text: 'second' })
    expect(s.turns.map((t) => t.pending)).toEqual(['sent', 'buffered'])
  })
})
