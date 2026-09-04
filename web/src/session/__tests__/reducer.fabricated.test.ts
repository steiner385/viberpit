import { describe, it, expect, vi } from 'vitest'
import { transcriptReducer, initialTranscript } from '../reducer'
import type { SessionEvent } from '../events'
import { clientLogger } from '../../obs/logger'

describe('transcript.turn.fabricated telemetry', () => {
  it('warns when a block event arrives for a turn no turnStart introduced', () => {
    const spy = vi.spyOn(clientLogger, 'warn').mockImplementation(() => {})
    const s = transcriptReducer({ ...initialTranscript, replaying: false },
      { type: 'textDelta', turnId: 'ghost', text: 'late' } as SessionEvent)
    expect(s.turns.map((t) => t.id)).toEqual(['ghost'])   // behaviour unchanged: still appended
    expect(spy).toHaveBeenCalledWith('transcript.turn.fabricated type=textDelta turnId=ghost')
    spy.mockRestore()
  })

  it('stays silent when the turn exists', () => {
    const spy = vi.spyOn(clientLogger, 'warn').mockImplementation(() => {})
    const s0 = transcriptReducer({ ...initialTranscript, replaying: false },
      { type: 'turnStart', turnId: 'a1' } as SessionEvent)
    const s1 = transcriptReducer(s0, { type: 'textDelta', turnId: 'a1', text: 'ok' } as SessionEvent)
    expect(s1.turns.map((t) => t.id)).toEqual(['a1'])
    expect(spy).not.toHaveBeenCalled()
    spy.mockRestore()
  })
})
