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

describe('fabricated turn re-anchors on its turnStart', () => {
  // A mid-turn reattach while an ask is pending (screenshot session bce4b626,
  // 2026-09-05 05:53): the edge re-emitted the pending question BEFORE the live
  // stream re-supplied the open turn, so the reducer fabricated the assistant turn
  // at the tail and the re-supplied prompt landed UNDER it. Every later delta then
  // grew the turn above the prompt — "my prompt is stuck at the bottom while the
  // answer scrolls past it".
  const RESET: SessionEvent = { type: 'replayReset' }
  const DONE: SessionEvent = { type: 'replayComplete' }
  const question: SessionEvent = {
    type: 'question', questionId: 'q1', turnId: 'X', header: 'Surfaces',
    question: 'Where?', options: [], multiSelect: false,
  }

  it('moves the fabricated turn below the prompt its turnStart follows', () => {
    const spy = vi.spyOn(clientLogger, 'warn').mockImplementation(() => {})
    const evs: SessionEvent[] = [
      RESET, DONE,
      question,                                                   // extras re-emit → fabricated X
      { type: 'user', turnId: 'U', text: 'oftentimes when I send…' }, // open-turn resupply
      { type: 'turnStart', turnId: 'X' },
      { type: 'textDelta', turnId: 'X', text: 'One more scoping question:' },
    ]
    const s = evs.reduce(transcriptReducer, initialTranscript)
    expect(s.turns.map((t) => t.id)).toEqual(['U', 'X'])
    const x = s.turns[1]
    expect(x.fabricated).toBeUndefined()
    expect(x.blocks.map((b) => b.kind)).toEqual(['question', 'text'])
    spy.mockRestore()
  })

  it('leaves a fabricated turn that is already last in place (same reference)', () => {
    const spy = vi.spyOn(clientLogger, 'warn').mockImplementation(() => {})
    const s0 = [RESET, DONE, { type: 'user', turnId: 'U', text: 'go' } as SessionEvent, question]
      .reduce(transcriptReducer, initialTranscript)
    expect(s0.turns[1].fabricated).toBe(true)
    const s1 = transcriptReducer(s0, { type: 'turnStart', turnId: 'X' })
    expect(s1.turns.map((t) => t.id)).toEqual(['U', 'X'])
    expect(s1.turns[1].fabricated).toBeUndefined()
    expect(s1.turns[1].blocks).toBe(s0.turns[1].blocks)
    spy.mockRestore()
  })

  it('never moves a turn its own turnStart introduced', () => {
    const evs: SessionEvent[] = [
      RESET, DONE,
      { type: 'turnStart', turnId: 'A' },
      { type: 'user', turnId: 'U', text: 'next' },
      { type: 'turnStart', turnId: 'A' },   // replay/resupply overlap: idempotent, stays put
    ]
    const s = evs.reduce(transcriptReducer, initialTranscript)
    expect(s.turns.map((t) => t.id)).toEqual(['A', 'U'])
  })
})
