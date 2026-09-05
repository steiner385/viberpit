// @vitest-environment node
/**
 * A session with a prompt in flight must not read as idle.
 *
 * Fire a prompt and go back to the list: the card kept showing "Your turn"/"Ready"
 * for the whole 30 s–2 min wake-up, which is the same "nothing happened" signal the
 * transcript used to give. This overlay is purely client-side truth (the store in
 * lib/promptFlight), deliberately NOT a new SessionStatus member — that would mean a
 * DB value, two Postgres compat-list entries and a migration for a fact that lives
 * for ninety seconds.
 */
import { describe, it, expect } from 'vitest'
import { statusPresentation } from './statusTier'

describe('statusPresentation — prompt in flight', () => {
  it('lifts an idle session out of the calm "Ready" pill', () => {
    const p = statusPresentation('idle', { promptStage: 'resuming' })
    expect(p.label).toBe('Sent — waking…')
    expect(p.tier).toBe('busy')
  })

  it('covers the "your turn" resting state too', () => {
    expect(statusPresentation('done', { promptStage: 'sent' }).label).toBe('Sending…')
  })

  it('leaves a session that is ALREADY working alone', () => {
    // The turn started; the send is old news and the real status is more informative.
    expect(statusPresentation('in_progress', { promptStage: 'delivered' }).label).toBe('Working…')
  })

  it('does not override a session that needs the user', () => {
    const p = statusPresentation('waiting_question', { promptStage: 'sent' })
    expect(p.label).toBe('Answer needed')
  })

  it('loses to the liveness demotion — an unreachable host cannot be "starting"', () => {
    const p = statusPresentation('idle', { promptStage: 'resuming', liveness: 'stale' })
    expect(p.tier).toBe('unreachable')
  })

  it('surfaces a stalled prompt as an error-tier pill', () => {
    const p = statusPresentation('idle', { promptStage: 'stalled' })
    expect(p.label).toBe('Prompt unacknowledged')
    expect(p.tier).toBe('error')
  })

  it('is unchanged when no prompt is in flight', () => {
    expect(statusPresentation('idle').label).toBe('Ready')
  })
})
