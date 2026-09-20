import { describe, it, expect, beforeEach } from 'vitest'
import { loadOrderPref, saveOrderPref, loadSessionOrder } from '../sessionOrder'
import { loadGroupCollapseNs, saveGroupCollapseNs, loadGroupCollapse } from '../groupCollapse'

beforeEach(() => window.localStorage.clear())

describe('namespaced order prefs', () => {
  it('live namespace uses the legacy key (back-compat)', () => {
    window.localStorage.setItem('viberpit.sessionOrder', JSON.stringify({ groupBy: 'repo', sortBy: 'name' }))
    expect(loadSessionOrder()).toEqual({ groupBy: 'repo', sortBy: 'name' })
  })

  it('a custom namespace is isolated and validates its own enums', () => {
    const G = ['none', 'account', 'repo'] as const
    const S = ['archived', 'created', 'name'] as const
    const def = { groupBy: 'none' as const, sortBy: 'archived' as const }
    saveOrderPref('archived', { groupBy: 'account', sortBy: 'name' })
    expect(loadOrderPref('archived', G, S, def)).toEqual({ groupBy: 'account', sortBy: 'name' })
    // unknown enum value falls back to default
    window.localStorage.setItem('viberpit.sessionOrder.archived', JSON.stringify({ groupBy: 'status', sortBy: 'zzz' }))
    expect(loadOrderPref('archived', G, S, def)).toEqual(def)
    // did not touch the live key
    expect(loadSessionOrder()).toEqual({ groupBy: 'none', sortBy: 'status' })
  })

  it('collapse keys are namespaced', () => {
    saveGroupCollapseNs('archived', new Set(['tony']))
    expect([...loadGroupCollapseNs('archived')]).toEqual(['tony'])
    expect([...loadGroupCollapse()]).toEqual([]) // live untouched
  })
})

// The brand rename (#1368) moved every key from `ccswitch.` to `viberpit.`. A
// user's prefs live in THEIR browser, so the flip has to be a read-through, not a
// reset — and it has to reach the per-list keys, whose names are built at runtime.
describe('pre-rebrand keys migrate on read', () => {
  it('reads a stored ordering written under the old namespace', () => {
    window.localStorage.setItem('ccswitch.sessionOrder',
      JSON.stringify({ groupBy: 'repo', sortBy: 'name' }))
    expect(loadSessionOrder()).toEqual({ groupBy: 'repo', sortBy: 'name' })
    expect(window.localStorage.getItem('ccswitch.sessionOrder')).toBeNull()
    expect(window.localStorage.getItem('viberpit.sessionOrder')).not.toBeNull()
  })

  it('reaches the per-list keys too', () => {
    const G = ['none', 'account'] as const
    const S = ['archived', 'name'] as const
    window.localStorage.setItem('ccswitch.sessionOrder.archived',
      JSON.stringify({ groupBy: 'account', sortBy: 'name' }))
    window.localStorage.setItem('ccswitch.groupCollapse.archived', JSON.stringify(['tony']))
    expect(loadOrderPref('archived', G, S, { groupBy: 'none', sortBy: 'archived' }))
      .toEqual({ groupBy: 'account', sortBy: 'name' })
    expect([...loadGroupCollapseNs('archived')]).toEqual(['tony'])
  })
})
