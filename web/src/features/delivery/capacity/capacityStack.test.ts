import { describe, it, expect } from 'vitest'
import type { CapacityReadout } from '@/api/capacity'
import { buildCapacityStack } from './capacityStack'

function readout(overrides: Partial<CapacityReadout> = {}): CapacityReadout {
  return {
    computedAt: '2026-09-19T10:00:00Z',
    pushLevel: 'steady',
    governorMode: 'monitor',
    killSwitch: true,
    global: { cap: 20, active: 10, free: 10 },
    hosts: [],
    accounts: { available: 2, total: 3, reservePct: 20, nextResetAt: null, rows: [] },
    runner: { ceiling: 5, inFlight: 3, derived: 4 },
    products: [],
    binding: null,
    defects: [],
    plan: null,
    ...overrides,
  }
}

describe('buildCapacityStack — Hosts row (fix round 1 / I4)', () => {
  it('counts only enabled, confirmed-healthy hosts toward used/cap/free', () => {
    const r = readout({
      hosts: [
        { name: 'dobby', enabled: true, healthy: true, active: 3, max: 10, free: 7 },
        { name: 'rocky', enabled: true, healthy: false, active: 9, max: 10, free: 1 },
        { name: 'stale', enabled: true, healthy: null, active: 5, max: 5, free: 0 },
        { name: 'off', enabled: false, healthy: true, active: 1, max: 5, free: 4 },
      ],
    })
    const hosts = buildCapacityStack(r).find((row) => row.key === 'hosts')!
    // Only 'dobby' is enabled AND healthy === true.
    expect(hosts.used).toBe(3)
    expect(hosts.cap).toBe(10)
    expect(hosts.free).toBe(7)
  })

  it('names the excluded count in the row secondary text', () => {
    const r = readout({
      hosts: [
        { name: 'dobby', enabled: true, healthy: true, active: 3, max: 10, free: 7 },
        { name: 'rocky', enabled: true, healthy: false, active: 9, max: 10, free: 1 },
        { name: 'stale', enabled: true, healthy: null, active: 5, max: 5, free: 0 },
      ],
    })
    const hosts = buildCapacityStack(r).find((row) => row.key === 'hosts')!
    expect(hosts.secondary).toBe('2 hosts excluded: disabled or unhealthy')
  })

  it('omits the secondary note when nothing is excluded', () => {
    const r = readout({
      hosts: [{ name: 'dobby', enabled: true, healthy: true, active: 3, max: 10, free: 7 }],
    })
    const hosts = buildCapacityStack(r).find((row) => row.key === 'hosts')!
    expect(hosts.secondary).toBeUndefined()
  })

  it('one excluded host is singular', () => {
    const r = readout({
      hosts: [
        { name: 'dobby', enabled: true, healthy: true, active: 3, max: 10, free: 7 },
        { name: 'rocky', enabled: false, healthy: true, active: 0, max: 10, free: 10 },
      ],
    })
    const hosts = buildCapacityStack(r).find((row) => row.key === 'hosts')!
    expect(hosts.secondary).toBe('1 host excluded: disabled or unhealthy')
  })

  it('an unbounded (max: null) counted host makes cap/free null, still excluding unhealthy hosts from the calculation', () => {
    const r = readout({
      hosts: [
        { name: 'dobby', enabled: true, healthy: true, active: 3, max: null, free: null },
        { name: 'rocky', enabled: true, healthy: false, active: 100, max: 1, free: 0 },
      ],
    })
    const hosts = buildCapacityStack(r).find((row) => row.key === 'hosts')!
    expect(hosts.cap).toBeNull()
    expect(hosts.free).toBeNull()
    expect(hosts.used).toBe(3)
    expect(hosts.secondary).toBe('1 host excluded: disabled or unhealthy')
  })
})
