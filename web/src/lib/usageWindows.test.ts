import { describe, it, expect } from 'vitest'
import type { AccountUsage } from '@/api/types'
import { bindingWindow, reserveWindow, usageKnown, usageUnavailableReason, usageWindows, windowByKey, windowsFor, windowLabel } from './usageWindows'
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

const kindash: AccountUsage = {
  provider: 'anthropic', fetchedAt: '2026-09-14T12:00:00Z', availability: 'available',
  reason: null, staleAsOf: null, extraUsageEnabled: false,
  windows: [
    { key: 'session', label: 'session', pct: 3, resetsAt: '2026-09-14T16:40:00Z', durationSeconds: 18000, scope: 'account', appliesToModels: [] },
    { key: 'weekly', label: 'weekly', pct: 91, resetsAt: '2026-09-14T20:00:00Z', durationSeconds: 604800, scope: 'account', appliesToModels: [] },
    { key: 'weekly:fable', label: 'Fable weekly', pct: 100, resetsAt: '2026-09-14T19:59:59Z', durationSeconds: 604800, scope: 'model', appliesToModels: ['fable'] },
  ],
}

describe('model-scoped windows', () => {
  it('applies a model window only to its own tier, and every window to an unknown model', () => {
    expect(windowsFor(kindash, 'sonnet').map((w) => w.key)).toEqual(['session', 'weekly'])
    expect(windowsFor(kindash, 'claude-fable-5-1').map((w) => w.key)).toEqual(['session', 'weekly', 'weekly:fable'])
    expect(windowsFor(kindash, null).map((w) => w.key)).toEqual(['session', 'weekly', 'weekly:fable'])
    expect(windowsFor(kindash, 'best').map((w) => w.key)).toEqual(['session', 'weekly', 'weekly:fable'])
  })
  it('binds per model', () => {
    expect(bindingWindow(kindash, 'sonnet')?.key).toBe('weekly')
    expect(bindingWindow(kindash, 'fable')?.key).toBe('weekly:fable')
    expect(bindingWindow(kindash)?.key).toBe('weekly:fable')
  })
  it('never measures a reserve on a model window', () => {
    expect(reserveWindow(kindash)?.key).toBe('weekly')
  })
  it('labels account windows by abbreviation and model windows by the provider label', () => {
    expect(windowLabel(kindash.windows[1])).toBe('wk')
    expect(windowLabel(kindash.windows[2])).toBe('Fable weekly')
    expect(windowLabel({ key: 'primary', label: 'primary', pct: 1, resetsAt: null })).toBe('primary')
  })
})
