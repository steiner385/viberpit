// @vitest-environment node
// Turn anchors (D-8 "Fork from here"): the server's `turnAnchor { turnId, uuid }` frame
// names the CLI transcript uuid a finished turn can be forked from. The reducer files it
// on that turn as `anchorUuid` — the UI offers Fork only on a turn that has one.
import { describe, expect, it } from 'vitest'
import { buildTurnsFromEvents, initialTranscript, transcriptReducer } from './reducer'
import type { LocalAction, TranscriptState } from './reducer'
import { SESSION_EVENT_TYPES } from './events'
import type { SessionEvent } from './events'

const reduce = (events: (SessionEvent | LocalAction)[], from: TranscriptState = initialTranscript) =>
  events.reduce(transcriptReducer, from)

const turn = (s: TranscriptState, id: string) => s.turns.find((t) => t.id === id)

describe('turnAnchor', () => {
  it('is a routed session event type', () => {
    expect(SESSION_EVENT_TYPES.has('turnAnchor')).toBe(true)
  })

  it('stores anchorUuid on the live assistant turn it names', () => {
    const s = reduce([
      { type: 'replayComplete' },
      { type: 'user', turnId: 'p1', text: 'go' },
      { type: 'turnStart', turnId: 'se-1' },
      { type: 'textDelta', turnId: 'se-1', text: 'done' },
      { type: 'turnAnchor', turnId: 'se-1', uuid: 'a3' },
      { type: 'turnEnd', turnId: 'se-1' },
      { type: 'user', turnId: 'p2', text: 'again' },
      { type: 'turnStart', turnId: 'se-2' },
      { type: 'textDelta', turnId: 'se-2', text: 'interrupted' },
      { type: 'turnEnd', turnId: 'se-2' },
    ])
    expect(turn(s, 'se-1')?.anchorUuid).toBe('a3')
    expect(turn(s, 'se-2')?.anchorUuid).toBeUndefined()
    expect(turn(s, 'p1')?.anchorUuid).toBeUndefined()
  })

  it('lands on the LAST segment of a steered turn — the reply that ends it', () => {
    const s = reduce([
      { type: 'replayComplete' },
      { type: 'user', turnId: 'p1', text: 'first' },
      { type: 'turnStart', turnId: 'se-1' },
      { type: 'textDelta', turnId: 'se-1', text: 'before steering' },
      { type: 'user', turnId: 'p2', text: 'steer' },
      { type: 'textDelta', turnId: 'se-1', text: 'after steering' },
      { type: 'turnAnchor', turnId: 'se-1', uuid: 'a-last' },
    ])
    const segments = s.turns.filter((t) => t.role === 'assistant')
    expect(segments).toHaveLength(2)
    expect(segments[0].anchorUuid).toBeUndefined()
    expect(segments[1].anchorUuid).toBe('a-last')
  })

  it('anchors the last replayed entry of a logical turn, not its earlier entries', () => {
    const s = reduce([
      { type: 'replayReset' },
      { type: 'user', turnId: 'u1', text: 'go' },
      { type: 'turnStart', turnId: 'a1' },
      { type: 'thinkingDelta', turnId: 'a1', text: 'hmm' },
      { type: 'turnEnd', turnId: 'a1' },
      { type: 'turnStart', turnId: 'a3' },
      { type: 'textDelta', turnId: 'a3', text: 'Done.' },
      { type: 'turnAnchor', turnId: 'a3', uuid: 'a3' },
      { type: 'turnEnd', turnId: 'a3' },
      { type: 'replayComplete' },
    ])
    expect(turn(s, 'a1')?.anchorUuid).toBeUndefined()
    expect(turn(s, 'a3')?.anchorUuid).toBe('a3')
  })

  it('ignores an anchor for a turn it does not hold (never fabricates one)', () => {
    const before = reduce([{ type: 'replayComplete' }, { type: 'turnStart', turnId: 'x' }])
    const after = transcriptReducer(before, { type: 'turnAnchor', turnId: 'not-loaded', uuid: 'u' })
    expect(after).toBe(before)
  })

  it('never anchors a USER turn sharing the id', () => {
    const before = reduce([{ type: 'replayComplete' }, { type: 'user', turnId: 'same', text: 'hi' }])
    const after = transcriptReducer(before, { type: 'turnAnchor', turnId: 'same', uuid: 'u' })
    expect(after).toBe(before)
  })

  it('survives an older-page prepend', () => {
    const turns = buildTurnsFromEvents([
      { type: 'user', turnId: 'u-old', text: 'old' },
      { type: 'turnStart', turnId: 'a-old' },
      { type: 'textDelta', turnId: 'a-old', text: 'old answer' },
      { type: 'turnAnchor', turnId: 'a-old', uuid: 'a-old' },
      { type: 'turnEnd', turnId: 'a-old' },
    ])
    expect(turns.find((t) => t.id === 'a-old')?.anchorUuid).toBe('a-old')
  })
})
