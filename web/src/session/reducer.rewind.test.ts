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
const REWOUND: SessionEvent = { type: 'rewound', rewindId: 'r1', toTurnId: 'u2', filesRestored: ['a.ts', 'b.ts'] }

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

  it('cuts nothing for a target this view does not hold, but still shows the divider', () => {
    const s = transcriptReducer(live, { ...REWOUND, toTurnId: 'elsewhere' })
    expect(s.turns).toBe(live.turns)
    expect(s.rewind?.rewindId).toBe('r1')
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

  it('on rewindUndone for its rewind', () => {
    expect(transcriptReducer(rewound, { type: 'rewindUndone', rewindId: 'r1' }).rewind).toBeNull()
  })

  it('not on rewindUndone for another rewind', () => {
    expect(transcriptReducer(rewound, { type: 'rewindUndone', rewindId: 'old' })).toBe(rewound)
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
