// @vitest-environment node
import { describe, it, expect } from 'vitest'
import {
  AUTOMATION_ROUTE, CI_REPO_ROUTE, DELIVERY_PATH, automationKey, automationPath, ciRepoKey, ciRepoPath,
  deliveryOpenItem, deliveryPath, deliveryScope, deliveryScopeForSearch, keyForDeliveryItem,
} from './deliveryNavState'

describe('deliveryNavState', () => {
  it('spells the paths once', () => {
    expect(DELIVERY_PATH).toBe('/delivery')
    expect(AUTOMATION_ROUTE).toBe('/delivery/automations/:automationId')
    expect(CI_REPO_ROUTE).toBe('/delivery/ci/:owner/:repo')
    expect(deliveryPath()).toBe('/delivery')
    expect(deliveryPath('automations')).toBe('/delivery')
    expect(deliveryPath('ci')).toBe('/delivery?scope=ci')
    expect(automationPath('pr-doctor')).toBe('/delivery/automations/pr-doctor')
    expect(ciRepoPath('cairnea/KinDash')).toBe('/delivery/ci/cairnea/KinDash')
  })
  it('reads the scope from ?scope=, defaulting to automations', () => {
    expect(deliveryScopeForSearch('')).toBe('automations')
    expect(deliveryScopeForSearch('?scope=ci')).toBe('ci')
    expect(deliveryScopeForSearch('?scope=nope')).toBe('automations')
  })
  it('a detail path implies its scope, whatever the search says', () => {
    expect(deliveryScope('/delivery/ci/o/r', '')).toBe('ci')
    expect(deliveryScope('/delivery/automations/pr-doctor', '?scope=ci')).toBe('automations')
    expect(deliveryScope('/delivery', '?scope=ci')).toBe('ci')
  })
  it('resolves the open item by PARAM NAME only', () => {
    expect(deliveryOpenItem({ automationId: 'pr-doctor' }, new URLSearchParams())).toEqual({ kind: 'automation', automationId: 'pr-doctor' })
    expect(deliveryOpenItem({ owner: 'o', repo: 'r' }, new URLSearchParams())).toEqual({ kind: 'ciRepo', repo: 'o/r' })
    expect(deliveryOpenItem({ id: 'x' }, new URLSearchParams())).toBeNull()
    expect(deliveryOpenItem({}, new URLSearchParams('?scope=ci'))).toBeNull()
  })
  it('keys rows the way the open item is keyed', () => {
    expect(keyForDeliveryItem({ kind: 'automation', automationId: 'a' })).toBe(automationKey('a'))
    expect(keyForDeliveryItem({ kind: 'ciRepo', repo: 'o/r' })).toBe(ciRepoKey('o/r'))
    expect(keyForDeliveryItem(null)).toBeNull()
  })
})
