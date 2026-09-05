import { describe, it, expect } from 'vitest'
import { NAV_DEFS } from '@/shell/navDefs'
import { STARTERS, startersFor } from './starters'

describe('starters', () => {
  it('covers every nav section with at least 2 chips', () => {
    for (const d of NAV_DEFS) {
      expect(STARTERS[d.id].length, `${d.id} starters`).toBeGreaterThanOrEqual(2)
    }
  })

  it('returns session-specific chips when a session is open', () => {
    expect(startersFor('/sessions/abc', 'abc')).toContain('Why is this session stuck?')
    // Open-session chips take over regardless of which section the route names.
    expect(startersFor('/pipeline', 'abc')).toContain('Why is this session stuck?')
  })

  it('returns the route section\'s general chips when no session is open', () => {
    expect(startersFor('/sessions', null)).toContain('What needs my attention?')
    expect(startersFor('/pipeline', null)).toContain('Which PRs are blocked, and why?')
  })

  it('falls back to the sessions set for an unmapped route', () => {
    expect(startersFor('/nowhere', null)).toEqual(STARTERS.sessions)
  })
})
