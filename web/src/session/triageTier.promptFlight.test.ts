// @vitest-environment node
import { describe, it, expect } from 'vitest'
import type { Session } from '@/api/types'
import { sessionTriage, triageTier } from './triageTier'

const session = (over: Partial<Session> = {}): Session => ({
  id: 's1', name: 's1', status: 'idle', account: 'alpha', liveness: 'fresh',
  ...over,
} as Session)

describe('sessionTriage — prompt in flight', () => {
  it('shows the in-flight pill on the card', () => {
    expect(sessionTriage(session(), 'resuming').label).toBe('Sent — waking…')
  })

  it('does NOT move the session between groups', () => {
    // The overlay maps to the `busy` tier, which sorts LAST — letting it drive
    // grouping would fling the card to the bottom of the list at the exact moment
    // the user is watching it, then fling it back ~90 s later. The pill carries the
    // news; the card stays put.
    expect(triageTier(session())).toBe('your-turn')
  })

  it('is unchanged with no prompt in flight', () => {
    expect(sessionTriage(session()).label).toBe('Ready')
  })
})
