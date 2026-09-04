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

  it('warns when an owner-prefix user turn is appended onto loaded content', () => {
    const spy = vi.spyOn(clientLogger, 'warn').mockImplementation(() => {})
    const s0 = transcriptReducer({ ...initialTranscript, replaying: false },
      { type: 'turnStart', turnId: 'a1', turnIndex: 1 } as SessionEvent)
    // A reattach's ring resupply re-delivers a window's owning prompt. Its real position
    // is not loaded, so it lands at the TAIL — the user-turn twin of a fabricated turn.
    const s1 = transcriptReducer(s0,
      { type: 'user', turnId: 'U', text: 'own', ownerPrefix: true } as SessionEvent)
    expect(s1.turns.map((t) => t.id)).toEqual(['a1', 'U'])   // behaviour unchanged: still appended
    expect(spy).toHaveBeenCalledWith('transcript.turn.fabricated type=user turnId=U')
    spy.mockRestore()
  })

  it('stays silent for an owner-prefix turn that opens a rebuilt transcript', () => {
    // The normal path: a replayReset empties `turns`, so the window's prefix legitimately
    // arrives first and is exactly where it belongs.
    const spy = vi.spyOn(clientLogger, 'warn').mockImplementation(() => {})
    transcriptReducer({ ...initialTranscript, replaying: true },
      { type: 'user', turnId: 'U', text: 'own', ownerPrefix: true } as SessionEvent)
    expect(spy).not.toHaveBeenCalled()
    spy.mockRestore()
  })

  it('stays silent for an ordinary user turn appended at the tail', () => {
    const spy = vi.spyOn(clientLogger, 'warn').mockImplementation(() => {})
    const s0 = transcriptReducer({ ...initialTranscript, replaying: false },
      { type: 'turnStart', turnId: 'a1' } as SessionEvent)
    transcriptReducer(s0, { type: 'user', turnId: 'U', text: 'next prompt' } as SessionEvent)
    expect(spy).not.toHaveBeenCalled()
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
