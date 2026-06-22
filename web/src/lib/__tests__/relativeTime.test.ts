import { describe, it, expect } from 'vitest'
import { relTime } from '../relativeTime'

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
