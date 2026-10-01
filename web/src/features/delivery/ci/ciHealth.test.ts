// @vitest-environment node
import { describe, it, expect } from 'vitest'
import type { Automation } from '@/api/automations'
import type { CheckInsights } from '@/api/types'
import { ciHealth, ciThresholds, flakeStatus } from './ciHealth'

const doctor = (over: Partial<Automation> = {}): Automation => ({
  id: 'cicd-doctor', name: 'CI/CD Doctor', description: '', enabled: true, mode: 'file', modes: ['off', 'file'],
  status: { text: 'Idle', level: 'idle', counts: null }, actionableCount: 0,
  configSchema: [
    { key: 'max_flake_pct', label: '', type: 'int', default: 2, help: '', choices: null },
    { key: 'demote_flake_pct', label: '', type: 'int', default: 5, help: '', choices: null },
  ],
  config: {}, lastActiveAt: null, activeSessions: 0, ...over,
} as Automation)

const insights = (over: Partial<CheckInsights> = {}): CheckInsights => ({
  repo: 'o/r', window: '7d',
  slowest: [{ check_name: 'e2e', event: 'pull_request', p50: 100, p90: 400, ratio: 4, n: 9 }],
  flakiest: [], regressions: [], runner_wait_health: [], ...over,
})
const flake = (pct: number) => ({ check_name: `c${pct}`, event: 'pull_request', flake_pct: pct, flaky_commits: 1, total_commits: 10, n: 10 })

describe('ciThresholds', () => {
  // The CI/CD Doctor's numbers are the single source of what "flaky" means:
  // its config when set, its schema defaults otherwise, nothing restated here.
  it('reads config over schema defaults from the CI/CD Doctor row, and is null without it', () => {
    expect(ciThresholds([doctor()])).toEqual({ maxFlakePct: 2, demoteFlakePct: 5 })
    expect(ciThresholds([doctor({ config: { demote_flake_pct: 8 } })])).toEqual({ maxFlakePct: 2, demoteFlakePct: 8 })
    expect(ciThresholds([])).toBeNull()
    expect(ciThresholds(undefined)).toBeNull()
    expect(ciThresholds([doctor({ configSchema: [] })])).toBeNull()
  })
})

describe('ciHealth', () => {
  const t = { maxFlakePct: 2, demoteFlakePct: 5 }
  it('is idle with no check history, green with history and nothing to say', () => {
    expect(ciHealth(undefined, t)).toEqual({ status: 'idle', reasons: [] })
    expect(ciHealth(insights({ slowest: [] }), t)).toEqual({ status: 'idle', reasons: [] })
    expect(ciHealth(insights(), t)).toEqual({ status: 'green', reasons: [] })
  })
  it('watches a flake at the graduation ceiling or a regression; is red at the demotion rate — worst wins', () => {
    expect(ciHealth(insights({ flakiest: [flake(2)] }), t)).toEqual({ status: 'amber', reasons: ['1 flaky ≥2%'] })
    expect(ciHealth(insights({ flakiest: [flake(1.9)] }), t)).toEqual({ status: 'green', reasons: [] })
    expect(ciHealth(insights({ regressions: [{ check_name: 'b', event: 'push', prior_p50: 1, recent_p50: 2, ratio: 2, since: '' }] }), t))
      .toEqual({ status: 'amber', reasons: ['1 regression'] })
    expect(ciHealth(insights({ flakiest: [flake(5), flake(20), flake(3)] }), t))
      .toEqual({ status: 'red', reasons: ['2 flaky ≥5%', '1 flaky ≥2%'] })
  })
  it('reports nothing about flake rates while the thresholds are unknown, but still watches a regression', () => {
    expect(ciHealth(insights({ flakiest: [flake(50)] }), null)).toEqual({ status: 'green', reasons: [] })
    expect(ciHealth(insights({ flakiest: [flake(50)], regressions: [{ check_name: 'b', event: 'push', prior_p50: 1, recent_p50: 2, ratio: 2, since: '' }] }), null))
      .toEqual({ status: 'amber', reasons: ['1 regression'] })
  })
  it('tones one flake rate the same way', () => {
    expect(flakeStatus(0.5, t)).toBe('green')
    expect(flakeStatus(2, t)).toBe('amber')
    expect(flakeStatus(5, t)).toBe('red')
    expect(flakeStatus(50, null)).toBe('idle')
  })
})
