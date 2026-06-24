import { describe, it, expect } from 'vitest'
import type { EvToolCall, EvTextDelta } from './events'
import { SESSION_EVENT_TYPES } from './events'

describe('SESSION_EVENT_TYPES', () => {
  it('includes sessionMeta so the WS fan-out routes it to the reducer', () => {
    expect(SESSION_EVENT_TYPES.has('sessionMeta')).toBe(true)
  })

  it('includes contextUsage so the exact ctx% readout reaches the reducer', () => {
    expect(SESSION_EVENT_TYPES.has('contextUsage')).toBe(true)
  })
})

describe('parentToolUseId field', () => {
  it('is carried on EvToolCall and EvTextDelta', () => {
    const tc: EvToolCall = {
      type: 'toolCall', turnId: 't', toolId: 'g1', name: 'Grep', toolKind: 'search',
      input: {}, parentToolUseId: 'task1',
    }
    const td: EvTextDelta = { type: 'textDelta', turnId: 't', text: 'hi', parentToolUseId: 'task1' }
    expect(tc.parentToolUseId).toBe('task1')
    expect(td.parentToolUseId).toBe('task1')
  })
})
