// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { dispatchOutcomeText, heldFor } from './holdOutcome'

describe('heldFor', () => {
  it('is coarse: hours under two days, days after', () => {
    expect(heldFor(null)).toBeNull()
    expect(heldFor(0.4)).toBe('held under an hour')
    expect(heldFor(3.4)).toBe('held 3 h')
    expect(heldFor(47)).toBe('held 47 h')
    expect(heldFor(290.9)).toBe('held 12 days')
  })
})

describe('dispatchOutcomeText', () => {
  it('is silent for a running session and explicit for the two non-running outcomes', () => {
    expect(dispatchOutcomeText({ status: 'spawned', approval: 'approved' }, 'fable')).toBeNull()
    expect(dispatchOutcomeText({ status: 'spawned', approval: 'not_needed' })).toBeNull()
    expect(dispatchOutcomeText({ status: 'busy' })).toBe(
      "Dispatch failed: busy. The hold was released; the next run uses the product's default model.")
    expect(dispatchOutcomeText({ status: 'spawned', approval: 'pending' }, 'fable')).toBe(
      'The session is parked for approval at Fable — approve it on the Sessions page.')
  })
})
