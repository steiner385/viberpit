import { describe, it, expect } from 'vitest'
import type { AccountUsage } from '@/api/types'
import { bindingWindow, reserveWindow, usageKnown, usageUnavailableReason, usageWindows, windowByKey } from './usageWindows'
import { anthropicUsage, copilotUsage } from '@/test/factories'

function unavailable(availability: AccountUsage['availability']): AccountUsage {
  return { provider: 'anthropic', fetchedAt: null, availability, windows: [], staleAsOf: null }
}

describe('usageWindows', () => {
  it('lists the windows a provider actually reported', () => {
    expect(usageWindows(anthropicUsage(10, 20)).map((w) => w.key)).toEqual(['session', 'weekly'])
    // Copilot meters ONE allowance — the old two-field wire shape duplicated it.
    expect(usageWindows(copilotUsage(20)).map((w) => w.key)).toEqual(['premium'])
  })

  it('treats an unavailable read as no windows, never as 0% used', () => {
    const failed: AccountUsage = {
      provider: 'anthropic', fetchedAt: null, availability: 'auth_error', windows: [],
      staleAsOf: null,
    }
    expect(usageWindows(failed)).toEqual([])
    expect(usageKnown(failed)).toBe(false)
    expect(bindingWindow(failed)).toBeNull()
    expect(usageKnown(null)).toBe(false)
  })

  it('ignores windows on a snapshot whose availability is not available', () => {
    // Belt and braces: even if a stale windows array rides along, an
    // unavailable snapshot must not be read as headroom.
    const stale = anthropicUsage(5, 5, { availability: 'unknown' })
    expect(usageWindows(stale)).toEqual([])
  })

  it('binds on the most-used window, ties going to the earlier one', () => {
    expect(bindingWindow(anthropicUsage(10, 80))?.key).toBe('weekly')
    expect(bindingWindow(anthropicUsage(80, 10))?.key).toBe('session')
    expect(bindingWindow(anthropicUsage(50, 50))?.key).toBe('session')
  })

  it('measures a reserve on the longest window, or the only one', () => {
    expect(reserveWindow(anthropicUsage(10, 20))?.key).toBe('weekly')
    expect(reserveWindow(copilotUsage(20))?.key).toBe('premium')
  })

  it('looks a window up by key', () => {
    expect(windowByKey(anthropicUsage(10, 20), 'weekly')?.pct).toBe(20)
    expect(windowByKey(copilotUsage(20), 'weekly')).toBeNull()
  })

  // A blank badge row could not distinguish "poll hasn't landed" from "this
  // credential can never report quota" — the state a setup-token account sits in.
  it('names why there are no windows, and stays silent when there are', () => {
    expect(usageUnavailableReason(anthropicUsage(10, 20))).toBeNull()
    expect(usageUnavailableReason(null)).toBe('Usage not read yet')
    expect(usageUnavailableReason(unavailable('unsupported')))
      .toBe('Quota not reported for this credential')
    expect(usageUnavailableReason(unavailable('auth_error')))
      .toBe('Credentials rejected — re-authenticate')
    expect(usageUnavailableReason(unavailable('unknown'))).toBe('Usage unavailable')
  })

  // 'unsupported' covers two very different situations: a provider with no quota
  // API (nothing to do) and an Anthropic setup-token minted without the scope the
  // usage endpoint reads (re-authenticate). The server names which via `reason`.
  it('tells the user to re-authenticate when the credential lacks the usage scope', () => {
    expect(usageUnavailableReason({ ...unavailable('unsupported'), reason: 'setup_token_scope' }))
      .toBe('Setup-token lacks the user:profile scope — re-authenticate to restore usage reporting')
    // An unrecognised reason falls back to the generic availability wording.
    expect(usageUnavailableReason({ ...unavailable('unsupported'), reason: 'something_new' }))
      .toBe('Quota not reported for this credential')
    expect(usageUnavailableReason({ ...unavailable('unsupported'), reason: null }))
      .toBe('Quota not reported for this credential')
  })
})
