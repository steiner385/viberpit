import { describe, it, expect } from 'vitest'
import { statusTier, statusPresentation, sortSessionsByTier } from '../statusTier'

describe('statusTier', () => {
  it.each([
    ['in_progress', 'busy'],
    ['waiting_input', 'needs-you'],
    ['waiting_question', 'needs-you'],
    ['ready_review', 'needs-you'],
    ['done', 'your-turn'],
    ['complete', 'your-turn'],
    ['idle', 'your-turn'],
    ['needs_attention', 'error'],
    ['limited', 'error'],
    ['ended', 'your-turn'],
    ['working', 'busy'],            // legacy alias tolerated
  ])('maps %s -> %s', (state, tier) => {
    expect(statusTier(state)).toBe(tier)
  })

  it('falls back to your-turn for an unknown state', () => {
    expect(statusTier('totally-unknown')).toBe('your-turn')
  })
})

describe('statusPresentation', () => {
  it.each([
    ['in_progress', 'Working…'],
    ['ready_review', 'Approve plan'],
    ['waiting_input', 'Permission needed'],
    ['waiting_question', 'Answer needed'],
    ['done', 'Your turn'],
    ['idle', 'Ready'],
    ['needs_attention', 'Turn failed'],
    ['limited', 'Usage limit'],
    ['ended', 'Ended'],
  ])('labels %s as "%s"', (state, label) => {
    expect(statusPresentation(state).label).toBe(label)
  })
})


describe('sortSessionsByTier', () => {
  it('sorts needs-you first, then error, your-turn, busy; stable within tier', () => {
    const input = [
      { id: 'a', name: 'A', status: 'in_progress' },
      { id: 'b', name: 'B', status: 'done' },
      { id: 'c', name: 'C', status: 'waiting_input' },
      { id: 'd', name: 'D', status: 'in_progress' },
      { id: 'e', name: 'E', status: 'needs_attention' },
    ]
    const result = sortSessionsByTier(input)
    expect(result.map((s) => s.id)).toEqual(['c', 'e', 'b', 'a', 'd'])
  })

  it('does not mutate the original array', () => {
    const input = [
      { id: 'x', status: 'in_progress' },
      { id: 'y', status: 'waiting_input' },
    ]
    const original = [...input]
    sortSessionsByTier(input)
    expect(input).toEqual(original)
  })
})
