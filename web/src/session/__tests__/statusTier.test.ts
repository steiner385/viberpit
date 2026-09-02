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
    ['waiting_external', 'waiting'],   // parked on an external event
    ['queued', 'waiting'],          // enqueued behind the active-session cap
    ['pending_approval', 'needs-you'],  // parked on a human model decision
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
    ['complete', 'Complete'],
    ['idle', 'Ready'],
    ['needs_attention', 'Turn failed'],
    ['limited', 'Usage limit'],
    ['ended', 'Ended'],
    ['waiting_external', 'Waiting…'],
    ['queued', 'Queued'],
    ['pending_approval', 'Awaiting approval'],
  ])('labels %s as "%s"', (state, label) => {
    expect(statusPresentation(state).label).toBe(label)
  })

  // Regression: a session that has not started yet used to hit the FALLBACK and
  // render as a calm "Ready" — indistinguishable from a live idle session, over an
  // empty conversation. That is what made a restart-stranded session read as a
  // healthy-but-dead orphan. 'queued'/'pending_approval' were in the SessionStatus
  // union but missing from the presentation table; PRESENTATION is now typed
  // Record<SessionStatus | 'working', …> so tsc fails the build if that drifts again.
  it.each(['queued', 'pending_approval'])(
    'never renders %s as the "Ready" fallback', (state) => {
      const p = statusPresentation(state)
      expect(p.label).not.toBe('Ready')
      expect(p.tier).not.toBe('your-turn')
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

  it('ranks waiting (parked) below your-turn and above busy', () => {
    const input = [
      { id: 'busy', status: 'in_progress' },
      { id: 'wait', status: 'waiting_external' },
      { id: 'turn', status: 'done' },
    ]
    const result = sortSessionsByTier(input)
    expect(result.map((s) => s.id)).toEqual(['turn', 'wait', 'busy'])
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
