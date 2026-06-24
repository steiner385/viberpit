import { describe, it, expect, beforeEach } from 'vitest'
import { loadOrderPref, saveOrderPref, loadSessionOrder } from '../sessionOrder'
import { loadGroupCollapseNs, saveGroupCollapseNs, loadGroupCollapse } from '../groupCollapse'

beforeEach(() => window.localStorage.clear())

describe('namespaced order prefs', () => {
  it('live namespace uses the legacy key (back-compat)', () => {
    window.localStorage.setItem('ccswitch.sessionOrder', JSON.stringify({ groupBy: 'repo', sortBy: 'name' }))
    expect(loadSessionOrder()).toEqual({ groupBy: 'repo', sortBy: 'name' })
  })

  it('a custom namespace is isolated and validates its own enums', () => {
    const G = ['none', 'account', 'repo'] as const
    const S = ['archived', 'created', 'name'] as const
    const def = { groupBy: 'none' as const, sortBy: 'archived' as const }
    saveOrderPref('archived', { groupBy: 'account', sortBy: 'name' })
    expect(loadOrderPref('archived', G, S, def)).toEqual({ groupBy: 'account', sortBy: 'name' })
    // unknown enum value falls back to default
    window.localStorage.setItem('ccswitch.sessionOrder.archived', JSON.stringify({ groupBy: 'status', sortBy: 'zzz' }))
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
