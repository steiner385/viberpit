// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from '../reducer'

const q = (over: Record<string, unknown> = {}) => ({
  type: 'question', questionId: 'toolu_A-0', turnId: 't1', header: 'Disposition',
  question: 'How should I dispose of #15205?', multiSelect: false,
  options: [{ label: 'Close it' }], ...over,
})

function questionBlock(state: ReturnType<typeof transcriptReducer>) {
  for (const t of state.turns) {
    for (const b of t.blocks) if (b.kind === 'question') return b
  }
  return null
}

describe('orphaned question', () => {
  it('a replayed question starts read-only', () => {
    let s = transcriptReducer(initialTranscript, { type: 'replayReset' } as never)
    s = transcriptReducer(s, q() as never)
    const b = questionBlock(s)
    expect(b?.kind === 'question' && b.live).toBeFalsy()
  })

  it('an orphaned re-offer upgrades the replayed card to live and marks it orphaned', () => {
    let s = transcriptReducer(initialTranscript, { type: 'replayReset' } as never)
    s = transcriptReducer(s, q() as never)
    s = transcriptReducer(s, { type: 'replayComplete' } as never)
    s = transcriptReducer(s, q({ orphaned: true }) as never)
    const b = questionBlock(s)
    expect(b?.kind === 'question' && b.live).toBe(true)
    expect(b?.kind === 'question' && b.orphaned).toBe(true)
  })

  it('does not append a duplicate card — the ids match replay', () => {
    let s = transcriptReducer(initialTranscript, { type: 'replayReset' } as never)
    s = transcriptReducer(s, q() as never)
    s = transcriptReducer(s, { type: 'replayComplete' } as never)
    s = transcriptReducer(s, q({ orphaned: true }) as never)
    const all = s.turns.flatMap((t) => t.blocks).filter((b) => b.kind === 'question')
    expect(all).toHaveLength(1)
  })

  it('never resurrects an already-answered question', () => {
    let s = transcriptReducer(initialTranscript, { type: 'replayReset' } as never)
    s = transcriptReducer(s, q() as never)
    s = transcriptReducer(s, { type: 'replayComplete' } as never)
    s = transcriptReducer(s, { type: 'questionResolved', questionId: 'toolu_A-0', turnId: 't1', selected: ['Close it'] } as never)
    s = transcriptReducer(s, q({ orphaned: true }) as never)
    const b = questionBlock(s)
    expect(b?.kind === 'question' && b.live).toBeFalsy()
  })
})
