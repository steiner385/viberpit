import { describe, it, expect } from 'vitest'
import { SESSION_EVENT_TYPES } from './events'

describe('SESSION_EVENT_TYPES', () => {
  it('includes sessionMeta so the WS fan-out routes it to the reducer', () => {
    expect(SESSION_EVENT_TYPES.has('sessionMeta')).toBe(true)
  })
})
