// @vitest-environment node
// Rewind to here (D-9): `rewound` cuts the target prompt and everything after it and puts
// the divider (`state.rewind`) in their place; `rewindUndone` and the next prompt drop it.
import { describe, expect, it } from 'vitest'
import { initialTranscript, transcriptReducer } from './reducer'
import type { LocalAction, TranscriptState } from './reducer'
import type { SessionEvent } from './events'

const reduce = (events: (SessionEvent | LocalAction)[], from: TranscriptState = initialTranscript) =>
  events.reduce(transcriptReducer, from)

const exchange = (u: string, a: string): SessionEvent[] => [
  { type: 'user', turnId: u, text: `ask ${u}` },
  { type: 'turnStart', turnId: a },
  { type: 'textDelta', turnId: a, text: `answer ${a}` },
  { type: 'turnEnd', turnId: a },
]

const live = reduce([{ type: 'replayComplete' }, ...exchange('u1', 'a1'), ...exchange('u2', 'a2'), ...exchange('u3', 'a3')])
const REWOUND: SessionEvent = { type: 'rewound', rewindId: 'r1', toTurnId: 'u2', promptUuid: 'P2', filesRestored: ['a.ts', 'b.ts'] }
const U2 = '22222222-2222-4222-8222-222222222222'

describe('rewound', () => {
  it('cuts the target prompt and every later turn, and sets the divider', () => {
    const s = transcriptReducer(live, REWOUND)
    expect(s.turns.map((t) => t.id)).toEqual(['u1', 'a1'])
    expect(s.rewind).toEqual({ rewindId: 'r1', filesRestored: ['a.ts', 'b.ts'] })
  })

  it('drops the rotations anchored to cut turns and keeps the rest', () => {
    const rotated = reduce([
      { type: '_rotation', from: 'x', to: 'y' },
    ], transcriptReducer(live, { type: 'user', turnId: 'u4', text: 'more' }))
    const withEarly = { ...rotated, rotations: [...rotated.rotations, { from: 'p', to: 'q', afterTurnId: 'a1' }, { from: 'p', to: 'q', afterTurnId: null }] }
    const s = transcriptReducer(withEarly, REWOUND)
    expect(s.rotations.map((r) => r.afterTurnId)).toEqual(['a1', null])
  })

  it('cuts a live turn (driver id + promptUuid) for an event keyed by the uuid, from a reloaded view', () => {
    const view = transcriptReducer(live, { type: 'userPromptUuid', turnId: 'u2', uuid: U2 })
    const s = transcriptReducer(view, { ...REWOUND, toTurnId: U2, promptUuid: U2 })
    expect(s.turns.map((t) => t.id)).toEqual(['u1', 'a1'])
    expect(s.rewind?.rewindId).toBe('r1')
  })

  it('cuts a replayed turn (id = uuid) for an event keyed by the live id, through its promptUuid', () => {
    const replayed = reduce([{ type: 'replayComplete' }, ...exchange('u1', 'a1'), ...exchange(U2, 'a2')])
    const s = transcriptReducer(replayed, { ...REWOUND, toTurnId: 'hex-live-id', promptUuid: U2 })
    expect(s.turns.map((t) => t.id)).toEqual(['u1', 'a1'])
    expect(s.rewind?.rewindId).toBe('r1')
  })

  it('an event without promptUuid matches only by toTurnId (an undefined key matches nothing)', () => {
    const { promptUuid: _omitted, ...bare } = REWOUND as Extract<SessionEvent, { type: 'rewound' }>
    void _omitted
    const s = transcriptReducer(live, bare)
    expect(s.turns.map((t) => t.id)).toEqual(['u1', 'a1'])
    expect(s.rewind?.rewindId).toBe('r1')
  })

  it('for a target this view does not hold: no cut, no divider, asks for a replay', () => {
    const s = transcriptReducer(live, { ...REWOUND, toTurnId: 'elsewhere', promptUuid: 'nowhere' })
    expect(s.turns).toBe(live.turns)
    expect(s.rewind).toBeNull()
    expect(s.replayFor).toBe('rewound:r1')
  })

  it('never cuts at an assistant turn sharing the id', () => {
    const s = transcriptReducer(live, { ...REWOUND, toTurnId: 'a2' })
    expect(s.turns).toBe(live.turns)
  })

  it('survives a replayReset (the replay never re-emits it)', () => {
    const s = reduce([REWOUND, { type: 'replayReset' }, ...exchange('u1', 'a1'), { type: 'replayComplete' }], live)
    expect(s.rewind?.rewindId).toBe('r1')
  })
})

describe('the divider goes', () => {
  const rewound = transcriptReducer(live, REWOUND)

  it('on rewindUndone for its rewind, which also asks for a replay', () => {
    const s = transcriptReducer(rewound, { type: 'rewindUndone', rewindId: 'r1' })
    expect(s.rewind).toBeNull()
    expect(s.replayFor).toBe('rewindUndone:r1')
  })

  it('not on rewindUndone for another rewind (which still asks for a replay)', () => {
    const s = transcriptReducer(rewound, { type: 'rewindUndone', rewindId: 'old' })
    expect(s.rewind?.rewindId).toBe('r1')
    expect(s.replayFor).toBe('rewindUndone:old')
  })

  it('when the server says the undo expired', () => {
    expect(transcriptReducer(rewound, { type: '_rewindExpired', rewindId: 'r1' }).rewind).toBeNull()
    expect(transcriptReducer(rewound, { type: '_rewindExpired', rewindId: 'old' })).toBe(rewound)
  })

  it('on the next send from this view', () => {
    expect(transcriptReducer(rewound, { type: '_trackSentPrompt', text: 'again' }).rewind).toBeNull()
  })

  it('on a new prompt sent live from another view', () => {
    expect(transcriptReducer(rewound, { type: 'user', turnId: 'u9', text: 'from my phone' }).rewind).toBeNull()
  })

  it('not on a prompt a replay rebuilds', () => {
    const s = reduce([{ type: 'replayReset' }, ...exchange('u1', 'a1')], rewound)
    expect(s.rewind?.rewindId).toBe('r1')
  })
})
