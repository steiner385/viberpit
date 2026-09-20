import { describe, it, expect } from 'vitest'
import type { CapacityReadout } from '@/api/capacity'
import { buildCapacityStack, accountRowLine } from './capacityStack'

function readout(overrides: Partial<CapacityReadout> = {}): CapacityReadout {
  return {
    computedAt: '2026-09-19T10:00:00Z',
    pushLevel: 'steady',
    governorMode: 'monitor',
    killSwitch: true,
    reservePctByLevel: { conserve: 40, steady: 20, max: 5 },
    global: { cap: 20, active: 10, free: 10, physical: null, attention: null, binding: null },
    hosts: [],
    accounts: { available: 2, total: 3, reservePct: 20, nextResetAt: null, rows: [] },
    runner: { ceiling: 5, effective: 5, inFlight: 3, derived: null },
    products: [],
    binding: null,
    defects: [],
    plan: null,
    planLive: false,
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

describe('buildCapacityStack — Runner row (fix round 2 / N1)', () => {
  it('uses the live plan-derived effective cap, not the stored ceiling, and notes the stored value', () => {
    const r = readout({
      runner: { ceiling: 6, effective: 3, inFlight: 2, derived: 3 },
    })
    const runner = buildCapacityStack(r).find((row) => row.key === 'runner')!
    expect(runner.cap).toBe(3)
    expect(runner.free).toBe(1)
    expect(runner.secondary).toBe('stored ceiling 6')
  })

  it('falls back to the stored ceiling with no secondary note when not live', () => {
    const r = readout({
      runner: { ceiling: 6, effective: 6, inFlight: 2, derived: null },
    })
    const runner = buildCapacityStack(r).find((row) => row.key === 'runner')!
    expect(runner.cap).toBe(6)
    expect(runner.free).toBe(4)
    expect(runner.secondary).toBeUndefined()
  })
})

describe('buildCapacityStack — row links (spec 2026-09-20 C4, D10)', () => {
  it('links each supply row to its Settings › Capacity section through settingsSectionPath', () => {
    const rows = Object.fromEntries(buildCapacityStack(readout()).map((row) => [row.key, row.href]))
    expect(rows.global).toBe('/settings/capacity')
    expect(rows.hosts).toBe('/settings/fleet')
    expect(rows.accounts).toBe('/settings/accounts')
  })
})

describe('buildCapacityStack — Global row (spec 2026-09-20 §5.2)', () => {
  it('names both ceilings and which one binds', () => {
    const r = readout({ global: { cap: 10, active: 5, free: 5, physical: 10, attention: 10, binding: 'attention' } })
    expect(buildCapacityStack(r).find((row) => row.key === 'global')!.secondary)
      .toBe('physical 10 · attention 10 · attention binds')
  })
  it('spells an unbounded physical ceiling and a missing attention ceiling', () => {
    const r = readout({ global: { cap: 5, active: 1, free: 4, physical: null, attention: null, binding: 'default' } })
    expect(buildCapacityStack(r).find((row) => row.key === 'global')!.secondary)
      .toBe('physical ∞ · attention none · default floor')
  })
  it('omits the secondary when the server has no derivation', () => {
    expect(buildCapacityStack(readout()).find((row) => row.key === 'global')!.secondary).toBeUndefined()
  })
  it('Products links to the table on the same page', () => {
    expect(buildCapacityStack(readout()).find((row) => row.key === 'products')!.href).toBe('#products')
  })
})

describe('accountRowLine (spec 2026-09-20 C21)', () => {
  const base = { name: 'acct', engine: 'claude_code', provider: 'anthropic', enabled: true, metered: true, available: true, reason: null, nextResetAt: null, windows: [] }
  it('available', () => { expect(accountRowLine(base)).toBe('acct — available') })
  it('unavailable with a reason', () => {
    expect(accountRowLine({ ...base, available: false, reason: 'at_ceiling' })).toBe('acct — at ceiling')
  })
  it('unavailable with a reason and a reset time', () => {
    const line = accountRowLine({ ...base, available: false, reason: 'at_ceiling', nextResetAt: new Date(Date.now() + 3_600_000).toISOString() })
    expect(line.startsWith('acct — at ceiling · resets ')).toBe(true)
  })
  it('not metered', () => {
    expect(accountRowLine({ ...base, engine: 'codex', metered: false, available: false, reason: 'not_metered' })).toBe('acct — not metered')
  })
  it('disabled', () => {
    expect(accountRowLine({ ...base, enabled: false, available: false, reason: 'disabled' })).toBe('acct — disabled')
  })
})
