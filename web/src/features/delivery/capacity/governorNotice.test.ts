import { describe, it, expect } from 'vitest'
import { CAPACITY } from './__fixtures__/capacity'
import { governorNotice } from './governorNotice'

describe('governorNotice (spec 2026-09-20 C18)', () => {
  it('kill switch off beats everything: plans are computed but never applied', () => {
    const n = governorNotice({ ...CAPACITY, governorMode: 'act', killSwitch: false })
    expect(n?.tone).toBe('warning')
    expect(n?.title).toBe('Governor kill switch is off')
    expect(n?.body).toContain('VIBERPIT_CAPACITY_GOVERNOR_ENABLED')
  })
  it('mode off: nothing is computed', () => {
    const n = governorNotice({ ...CAPACITY, governorMode: 'off', killSwitch: true })
    expect(n).toEqual({ tone: 'info', title: 'Governor is off', body: 'No plan is computed; the runner uses each product’s stored pin and the stored runner ceiling.' })
  })
  it('monitor: plan shown but not applied', () => {
    const n = governorNotice({ ...CAPACITY, governorMode: 'monitor', killSwitch: true })
    expect(n?.tone).toBe('info')
    expect(n?.title).toBe('Governor is in monitor mode')
  })
  it('act with a live plan: no notice', () => {
    expect(governorNotice({ ...CAPACITY, governorMode: 'act', killSwitch: true, planLive: true, plan: { ...CAPACITY.plan!, applied: true } })).toBeNull()
  })
  it('act with an applied but stale plan', () => {
    const n = governorNotice({ ...CAPACITY, governorMode: 'act', killSwitch: true, planLive: false, plan: { ...CAPACITY.plan!, applied: true } })
    expect(n?.tone).toBe('warning')
    expect(n?.title).toBe('The last plan is stale')
  })
  it('act with no plan yet', () => {
    const n = governorNotice({ ...CAPACITY, governorMode: 'act', killSwitch: true, planLive: false, plan: null })
    expect(n).toEqual({ tone: 'info', title: 'No plan yet', body: 'The governor has not ticked since it was enabled; stored caps apply until it does.' })
  })
})
