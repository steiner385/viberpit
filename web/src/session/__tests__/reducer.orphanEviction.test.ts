// @vitest-environment node
// web/src/session/__tests__/reducer.orphanEviction.test.ts
//
// An orphan toolResult evicted from the bounded FIFO is a result LOST FOREVER (its
// tool block renders resultless). The cap should never be hit in practice, so every
// eviction must reach the obs sink — a silent eviction reads as "tool never finished"
// with no trail to debug from.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { transcriptReducer, initialTranscript, ORPHAN_TOOL_RESULT_CAP } from '../reducer'
import { clientLogger } from '../../obs/logger'
import type { SessionEvent } from '../events'

afterEach(() => vi.restoreAllMocks())

const orphanResult = (i: number): SessionEvent =>
  ({ type: 'toolResult', toolId: `orphan-${i}`, ok: true, payload: {} }) as SessionEvent

describe('orphan toolResult eviction telemetry', () => {
  it('warns to the obs sink when the FIFO cap evicts the oldest orphan', () => {
    const spy = vi.spyOn(clientLogger, 'warn').mockImplementation(() => {})
    let s = initialTranscript
    for (let i = 0; i <= ORPHAN_TOOL_RESULT_CAP; i++) s = transcriptReducer(s, orphanResult(i))
    expect(Object.keys(s.orphanToolResults)).toHaveLength(ORPHAN_TOOL_RESULT_CAP)
    expect(s.orphanToolResults['orphan-0']).toBeUndefined() // oldest evicted
    expect(spy).toHaveBeenCalledWith(
      expect.stringContaining('transcript.orphan.evicted toolId=orphan-0'))
  })

  it('does not warn while the buffer is under the cap', () => {
    const spy = vi.spyOn(clientLogger, 'warn').mockImplementation(() => {})
    let s = initialTranscript
    for (let i = 0; i < ORPHAN_TOOL_RESULT_CAP; i++) s = transcriptReducer(s, orphanResult(i))
    expect(spy).not.toHaveBeenCalled()
  })
})
