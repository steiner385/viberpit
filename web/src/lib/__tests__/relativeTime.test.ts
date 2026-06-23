import { describe, it, expect } from 'vitest'
import { isValidTimestamp, relTime } from '../relativeTime'

const NOW = Date.parse('2026-06-22T12:00:00Z')
const ago = (ms: number) => new Date(NOW - ms).toISOString()

const SEC = 1000
const MIN = 60 * SEC
const HOUR = 60 * MIN
const DAY = 24 * HOUR

describe('relTime', () => {
  it('renders seconds under a minute', () => {
    expect(relTime(ago(5 * SEC), NOW)).toBe('5s ago')
  })

  it('renders minutes under an hour', () => {
    expect(relTime(ago(2 * MIN), NOW)).toBe('2m ago')
  })

  it('renders hours under a day', () => {
    expect(relTime(ago(3 * HOUR), NOW)).toBe('3h ago')
  })

  it('renders days beyond a day', () => {
    expect(relTime(ago(3 * DAY), NOW)).toBe('3d ago')
  })

  it('clamps future timestamps to 0s (never negative)', () => {
    expect(relTime(ago(-10 * SEC), NOW)).toBe('0s ago')
  })

  it('defaults the reference time to now when omitted', () => {
    // Just-now timestamp should read as a small number of seconds.
    expect(relTime(new Date().toISOString())).toMatch(/^\d+s ago$/)
  })
})

describe('isValidTimestamp', () => {
  it('rejects null, undefined, and empty string (no false 1970 label)', () => {
    expect(isValidTimestamp(null)).toBe(false)
    expect(isValidTimestamp(undefined)).toBe(false)
    expect(isValidTimestamp('')).toBe(false)
  })

  it('rejects the epoch-0 sentinel that null/"" coerce to', () => {
    expect(isValidTimestamp(new Date(0).toISOString())).toBe(false)
  })

  it('rejects unparseable strings', () => {
    expect(isValidTimestamp('not-a-date')).toBe(false)
  })

  it('accepts a real ISO timestamp', () => {
    expect(isValidTimestamp('2026-06-22T11:58:00Z')).toBe(true)
  })
})
