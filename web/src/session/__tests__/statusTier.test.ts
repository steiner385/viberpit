import { describe, it, expect } from 'vitest'
import {
  statusTier,
  statusPresentation,
  tierRank,
  sortByTier,
  type StatusTier,
} from '../statusTier'

describe('statusTier — rich status → tier + label', () => {
  it.each<[string, StatusTier, string]>([
    // busy
    ['in_progress', 'busy', 'Working…'],
    ['working', 'busy', 'Working…'], // legacy alias
    // needs-you
    ['waiting_input', 'needs-you', 'Permission needed'],
    ['ready_review', 'needs-you', 'Approve plan'],
    // your-turn
    ['done', 'your-turn', 'Your turn'],
    ['complete', 'your-turn', 'Your turn'],
    ['idle', 'your-turn', 'Ready'],
    // error
    ['needs_attention', 'error', 'Turn failed'],
    ['limited', 'error', 'Usage limit'],
    ['ended', 'error', 'Ended'],
  ])('%s → tier %s, label "%s"', (state, tier, label) => {
    expect(statusTier(state)).toBe(tier)
    expect(statusPresentation(state).label).toBe(label)
  })

  it('falls back to the calm your-turn tier for an unknown status', () => {
    expect(statusTier('something-weird')).toBe('your-turn')
    expect(statusPresentation('something-weird').label).toBe('Ready')
  })
})

describe('tierRank + sortByTier', () => {
  it('ranks needs-you and error above your-turn above busy', () => {
    expect(tierRank('waiting_input')).toBe(0)
    expect(tierRank('needs_attention')).toBe(0)
    expect(tierRank('done')).toBe(1)
    expect(tierRank('in_progress')).toBe(2)
  })

  it('orders a list needs-you/error → your-turn → busy', () => {
    const sessions = [
      { id: 'a', status: 'in_progress' },
      { id: 'b', status: 'done' },
      { id: 'c', status: 'waiting_input' },
      { id: 'd', status: 'needs_attention' },
    ]
    const sorted = sortByTier(sessions, (s) => s.status).map((s) => s.id)
    // c (needs-you) and d (error) share rank 0 → keep their relative input order;
    // then b (your-turn), then a (busy).
    expect(sorted).toEqual(['c', 'd', 'b', 'a'])
  })

  it('is stable within a tier (preserves input order)', () => {
    const sessions = [
      { id: 'x1', status: 'done' },
      { id: 'x2', status: 'idle' },
      { id: 'x3', status: 'complete' },
    ]
    const sorted = sortByTier(sessions, (s) => s.status).map((s) => s.id)
    expect(sorted).toEqual(['x1', 'x2', 'x3'])
  })

  it('does not mutate the input array', () => {
    const sessions = [
      { id: 'a', status: 'in_progress' },
      { id: 'b', status: 'waiting_input' },
    ]
    const before = sessions.map((s) => s.id)
    sortByTier(sessions, (s) => s.status)
    expect(sessions.map((s) => s.id)).toEqual(before)
  })
})
