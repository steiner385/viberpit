/**
 * Tests for the status-strip additions to transcriptReducer:
 * current model (sessionMeta) and per-result usage snapshot (turnEnd).
 */
import { describe, it, expect } from 'vitest'
import { transcriptReducer, initialTranscript } from './reducer'

describe('transcriptReducer — sessionMeta', () => {
  it('starts with model null', () => {
    expect(initialTranscript.model).toBeNull()
  })

  it('sets the current model from a sessionMeta event', () => {
    const s = transcriptReducer(initialTranscript, {
      type: 'sessionMeta', model: 'claude-sonnet-4-5', sessionId: 'abc',
    })
    expect(s.model).toBe('claude-sonnet-4-5')
  })
})

describe('transcriptReducer — usage snapshot', () => {
  it('starts with usage null', () => {
    expect(initialTranscript.usage).toBeNull()
  })

  it('snapshots the latest turn usage (snake_case → camelCase) and cost', () => {
    const s = transcriptReducer(initialTranscript, {
      type: 'turnEnd',
      turnId: 't1',
      usage: {
        input_tokens: 1_200,
        output_tokens: 800,
        cache_read_input_tokens: 30_000,
      },
      costUsd: 0.42,
    })
    expect(s.usage).toEqual({ input: 1_200, output: 800, cacheRead: 30_000, costUsd: 0.42 })
  })

  it('replaces (not accumulates) usage with the most recent turn', () => {
    const s1 = transcriptReducer(initialTranscript, {
      type: 'turnEnd', turnId: 't1',
      usage: { input_tokens: 100, output_tokens: 10, cache_read_input_tokens: 5 }, costUsd: 0.1,
    })
    const s2 = transcriptReducer(s1, {
      type: 'turnEnd', turnId: 't2',
      usage: { input_tokens: 50_000, output_tokens: 20, cache_read_input_tokens: 9 }, costUsd: 0.3,
    })
    expect(s2.usage).toEqual({ input: 50_000, output: 20, cacheRead: 9, costUsd: 0.3 })
  })

  it('leaves usage unchanged when a turnEnd carries no usage', () => {
    const s = transcriptReducer(initialTranscript, { type: 'turnEnd', turnId: 't1', usage: null })
    expect(s.usage).toBeNull()
  })
})
