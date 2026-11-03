// @vitest-environment node
// Prompt uuids (D-9 "Rewind to here"): the server's `userPromptUuid { turnId, uuid }` frame
// names the CLI prompt uuid of a LIVE user turn — the id a rewind targets. The reducer files
// it on that user turn as `promptUuid`. (On replay the user turn id already is the uuid.)
import { describe, expect, it } from 'vitest'
import { buildTurnsFromEvents, initialTranscript, transcriptReducer } from './reducer'
import type { LocalAction, TranscriptState } from './reducer'
import { SESSION_EVENT_TYPES } from './events'
import type { SessionEvent } from './events'

const reduce = (events: (SessionEvent | LocalAction)[], from: TranscriptState = initialTranscript) =>
  events.reduce(transcriptReducer, from)

const turn = (s: TranscriptState, id: string) => s.turns.find((t) => t.id === id)

describe('userPromptUuid', () => {
  it('is a routed session event type', () => {
    expect(SESSION_EVENT_TYPES.has('userPromptUuid')).toBe(true)
  })

  it('stores promptUuid on the live user turn it names', () => {
    const s = reduce([
      { type: 'replayComplete' },
      { type: 'user', turnId: 'p1', text: 'go' },
      { type: 'userPromptUuid', turnId: 'p1', uuid: 'U1' },
      { type: 'turnStart', turnId: 'se-1' },
      { type: 'textDelta', turnId: 'se-1', text: 'ok' },
      { type: 'turnEnd', turnId: 'se-1' },
    ])
    expect(turn(s, 'p1')?.promptUuid).toBe('U1')
    expect(turn(s, 'se-1')?.promptUuid).toBeUndefined()
  })

  it('never lands on an ASSISTANT turn sharing the id', () => {
    const before = reduce([{ type: 'replayComplete' }, { type: 'turnStart', turnId: 'same' }])
    const after = transcriptReducer(before, { type: 'userPromptUuid', turnId: 'same', uuid: 'u' })
    expect(after).toBe(before)
  })

  it('ignores a uuid for a user turn it does not hold (never fabricates one)', () => {
    const before = reduce([{ type: 'replayComplete' }, { type: 'user', turnId: 'p1', text: 'hi' }])
    const after = transcriptReducer(before, { type: 'userPromptUuid', turnId: 'missing', uuid: 'u' })
    expect(after).toBe(before)
  })

  it('is idempotent for a repeated frame (ring resupply)', () => {
    const before = reduce([
      { type: 'replayComplete' },
      { type: 'user', turnId: 'p1', text: 'hi' },
      { type: 'userPromptUuid', turnId: 'p1', uuid: 'U1' },
    ])
    expect(transcriptReducer(before, { type: 'userPromptUuid', turnId: 'p1', uuid: 'U1' })).toBe(before)
  })

  it('survives an older-page rebuild', () => {
    const turns = buildTurnsFromEvents([
      { type: 'user', turnId: 'p-old', text: 'old' },
      { type: 'userPromptUuid', turnId: 'p-old', uuid: 'U-old' },
      { type: 'turnStart', turnId: 'a-old' },
      { type: 'turnEnd', turnId: 'a-old' },
    ])
    expect(turns.find((t) => t.id === 'p-old')?.promptUuid).toBe('U-old')
  })
})
