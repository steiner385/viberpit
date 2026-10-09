import { describe, expect, it } from 'vitest'
import { relativeAge, shortName, toneFor, worstStatus } from './mcpStatus'

describe('worstStatus', () => {
  it('ranks failed > needs-auth > pending > connected', () => {
    expect(worstStatus(['connected', 'needs-auth', 'pending'])).toBe('needs-auth')
    expect(worstStatus(['connected', 'failed'])).toBe('failed')
  })
  it('returns null for no rows', () => expect(worstStatus([])).toBeNull())
  it('keeps unknown statuses but ranks them below known problems', () => {
    expect(worstStatus(['connecting'])).toBe('connecting')
    expect(worstStatus(['connecting', 'failed'])).toBe('failed')
  })
})

describe('toneFor', () => {
  it('maps the vocabulary', () => {
    expect(toneFor('failed')).toBe('bad'); expect(toneFor('needs-auth')).toBe('warn')
    expect(toneFor('pending')).toBe('pending'); expect(toneFor('connected')).toBe('ok')
    expect(toneFor('disabled')).toBe('neutral'); expect(toneFor('connecting')).toBe('neutral')
    expect(toneFor(null)).toBe('unknown')
  })
})

it('shortName strips plugin prefix', () => {
  expect(shortName('plugin:railway:railway')).toBe('railway')
  expect(shortName('github')).toBe('github')
})

it('relativeAge reuses the shared relative-time formatter', () => {
  const now = Date.parse('2026-10-09T12:05:00Z')
  expect(relativeAge('2026-10-09T12:02:00Z', now)).toBe('3m ago')
})
