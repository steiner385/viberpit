import { describe, it, expect } from 'vitest'
import { statusTier, statusPresentation } from '../statusTier'

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
    ['ended', 'error'],
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
