import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript, buildTurnsFromEvents } from './reducer'
import type { SessionEvent } from './events'

const olderTurn: SessionEvent[] = [
  { type: 'user', turnId: 'u-old', text: 'old question' },
  { type: 'turnStart', turnId: 'a-old' },
  { type: 'textDelta', turnId: 'a-old', text: 'old answer' },
  { type: 'turnEnd', turnId: 'a-old' },
]

describe('history paging reducer', () => {
  it('records extent on replayComplete', () => {
    const s = transcriptReducer(initialTranscript,
      { type: 'replayComplete', earliestTurn: 5, totalTurns: 25 } as SessionEvent)
    expect(s.earliestTurn).toBe(5)
    expect(s.totalTurns).toBe(25)
    expect(s.replaying).toBe(false)
  })

  it('prepends older turns ahead of existing turns and lowers earliestTurn', () => {
    let s = { ...initialTranscript, earliestTurn: 5,
      turns: [{ id: 'a-cur', role: 'assistant' as const, blocks: [] }] }
    s = transcriptReducer(s, { type: '_prependHistory', events: olderTurn, earliestTurn: 4 } as never)
    expect(s.turns[0].id).toBe('u-old')   // older turn now first
    expect(s.turns[s.turns.length - 1].id).toBe('a-cur')
    expect(s.earliestTurn).toBe(4)
    expect(s.loadingOlder).toBe(false)
  })

  it('replayReset clears extent', () => {
    let s = { ...initialTranscript, earliestTurn: 5, totalTurns: 25 }
    s = transcriptReducer(s, { type: 'replayReset' } as SessionEvent)
    expect(s.earliestTurn).toBe(0)
    expect(s.totalTurns).toBe(0)
  })

  it('buildTurnsFromEvents reuses the reducer to produce turns', () => {
    const turns = buildTurnsFromEvents(olderTurn)
    expect(turns.map((t) => t.id)).toEqual(['u-old', 'a-old'])
  })
})
