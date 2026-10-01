// @vitest-environment node
import { describe, it, expect } from 'vitest'
import type { Automation } from '@/api/automations'
import { automationTier, automationTierLine } from './automationTier'

const base: Pick<Automation, 'enabled' | 'status' | 'activeSessions' | 'actionableCount'> = {
  enabled: true, status: { text: 'Idle', level: 'idle', counts: null }, activeSessions: 0, actionableCount: 0,
}

describe('automation attention', () => {
  it('uses actionable count while distinguishing known zero from unavailable counts', () => {
    expect(automationTier({ ...base, actionableCount: 2 })).toBe('needs-you')
    expect(automationTier(base)).toBe('idle')
    for (const actionableCount of [null, undefined]) {
      const a = { ...base, actionableCount }
      expect(automationTier(a)).toBe('needs-you')
      expect(automationTierLine(automationTier(a), a)).toBe('Actionable count unknown')
    }
  })
  // 2026-10-01 Delivery critique, P0: housekeeping is said, never tiered.
  it('says maintenance in the line but never tiers or badges on it', () => {
    const a = { ...base, actionableCount: 0, maintenanceCount: 47 }
    expect(automationTier(a)).toBe('idle')
    expect(automationTierLine(automationTier(a), a)).toBe('0 actionable · 47 maintenance')
    const b = { ...base, actionableCount: 7, maintenanceCount: 47 }
    expect(automationTier(b)).toBe('needs-you')
    expect(automationTierLine(automationTier(b), b)).toBe('7 actionable · 47 maintenance')
    expect(automationTierLine(automationTier(base), { ...base, maintenanceCount: 0 })).not.toMatch(/maintenance/)
  })

  it('preserves live sessions during expected schedule pauses without claiming blocked', () => {
    const a: typeof base = { ...base, activeSessions: 3, status: { text: 'Paused', level: 'warn', waitingReason: 'schedule', counts: null } }
    expect(automationTier(a)).toBe('in-flight')
    expect(automationTierLine(automationTier(a), a)).toBe('0 actionable · Waiting for schedule · 3 live sessions')
  })
  // 2026-10-01 Delivery critique, P2: "Reconcile refused" and "a defect, not
  // load" tiered idle and read calm. A warn with no typed wait is blocked.
  it('a warn with no waiting reason is blocked, and says so in the line', () => {
    const a: typeof base = { ...base, status: { text: 'Reconcile refused: master is 9 commits ahead', level: 'warn', counts: null } }
    expect(automationTier(a)).toBe('blocked')
    expect(automationTierLine(automationTier(a), a)).toBe('Blocked · 0 actionable')
    // Live sessions do not soften a defect into in-flight; the word still leads.
    const live = { ...a, activeSessions: 1 }
    expect(automationTier(live)).toBe('blocked')
    expect(automationTierLine(automationTier(live), live)).toBe('Blocked · 0 actionable · 1 live session')
  })
  it('a warn with a typed wait is an expected pause, not blocked; actionable items outrank it; a switched-off loop is idle', () => {
    const paused: typeof base = { ...base, status: { text: 'Capacity limit reached', level: 'warn', waitingReason: 'policy', counts: null } }
    expect(automationTier(paused)).toBe('idle')
    expect(automationTierLine(automationTier(paused), paused)).toBe('0 actionable · Waiting for policy')
    const decided = { ...base, actionableCount: 2, status: { text: 'Budget reached', level: 'warn' as const, counts: null } }
    expect(automationTier(decided)).toBe('needs-you')
    expect(automationTierLine(automationTier(decided), decided)).toBe('2 actionable · Notice')
    const off = { ...base, enabled: false, status: { text: 'Budget reached', level: 'warn' as const, counts: null } }
    expect(automationTier(off)).toBe('idle')
    expect(automationTierLine(automationTier(off), off)).toBe('0 actionable · Notice')
  })
  it('human waits need attention without being service errors or requiring action buttons', () => {
    const a: typeof base = { ...base, status: { text: 'Approval required', level: 'idle', waitingReason: 'human', counts: null } }
    expect(automationTier(a)).toBe('needs-you')
    expect(automationTierLine(automationTier(a), a)).toBe('0 actionable · Waiting for human')
  })
  it('disabled human decisions still need attention without live sessions or action items', () => {
    const a: typeof base = { ...base, enabled: false, status: { text: 'Approval required', level: 'idle', waitingReason: 'human', counts: null } }
    expect(automationTier(a)).toBe('needs-you')
    expect(automationTierLine(automationTier(a), a)).toContain('Waiting for human')
    expect(automationTier({ ...a, activeSessions: 1 })).toBe('needs-you')
    expect(automationTier({ ...a, actionableCount: 1 })).toBe('needs-you')
  })
  it.each(['schedule', 'capacity', 'budget'] as const)('%s waits do not imply human attention', (waitingReason) => {
    expect(automationTier({ ...base, status: { ...base.status, waitingReason } })).toBe('idle')
  })
  it('keeps unknown status and true errors visible', () => {
    expect(automationTier({ ...base, status: { text: 'Unavailable', level: 'unknown', counts: null } })).toBe('needs-you')
    expect(automationTier({ ...base, status: { text: 'Failed', level: 'error', counts: null } })).toBe('needs-you')
  })
})
