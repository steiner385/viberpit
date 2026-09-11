// @vitest-environment node
import { describe, it, expect } from 'vitest'
import type { AutomationStatus } from '@/api/automations'
import { automationTier, automationTierLine } from './automationTier'

// `status` is typed as the full AutomationStatus union (not narrowed to 'idle' via
// `as const`) so a per-test override can swap in any level without a TS2322.
const base: { enabled: boolean; status: AutomationStatus; activeSessions: number } = {
  enabled: true, status: { text: 'Idle', level: 'idle', counts: null }, activeSessions: 0,
}
const at = (over: Partial<typeof base>, actionable = 0) => automationTier({ ...base, ...over }, actionable)

describe('automationTier', () => {
  it('needs-you when there is something to act on or the loop errored', () => {
    expect(at({}, 2)).toBe('needs-you')
    expect(at({ status: { text: 'boom', level: 'error', counts: null } })).toBe('needs-you')
  })
  it('blocked when enabled but paused/degraded (warn)', () => {
    expect(at({ status: { text: 'paused — outside scheduled active hours', level: 'warn', counts: null } })).toBe('blocked')
    expect(at({ enabled: false, status: { text: 'x', level: 'warn', counts: null } })).toBe('idle')
  })
  it('in-flight when it has live sessions or is active', () => {
    expect(at({ activeSessions: 1 })).toBe('in-flight')
    expect(at({ status: { text: 'scanning', level: 'active', counts: null } })).toBe('in-flight')
  })
  it('needs-you outranks everything', () => {
    expect(at({ activeSessions: 3 }, 1)).toBe('needs-you')
  })
  it('line 2 names the tier and the actionable count', () => {
    expect(automationTierLine('needs-you', { ...base }, 2)).toBe('Needs you · 2 actionable')
    expect(automationTierLine('in-flight', { ...base, activeSessions: 1 }, 0)).toBe('In flight · 1 live session')
    expect(automationTierLine('idle', base, 0)).toBe('Idle')
  })
})
