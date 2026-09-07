// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { INITIATIVE_STAGES } from '@/api/sdlc'
import { PROJECT_ONBOARDING_STATES } from '@/api/pm'
import {
  PRODUCT, CAPABILITY, STAGE_LABEL, stageLabel,
  INITIATIVE_STATUS_LABEL, initiativeStatusLabel,
  ONBOARDING_STATE_LABEL, onboardingStateLabel,
} from './vocab'

describe('vocab', () => {
  it('names every initiative stage', () => {
    for (const s of INITIATIVE_STAGES) expect(STAGE_LABEL[s].length).toBeGreaterThan(0)
    expect(stageLabel('intake')).toBe('Intent')
    expect(stageLabel('design')).toBe('Spec')
  })

  it('names every onboarding state', () => {
    for (const s of PROJECT_ONBOARDING_STATES) expect(ONBOARDING_STATE_LABEL[s].length).toBeGreaterThan(0)
    expect(onboardingStateLabel('map_proposed')).toBe('Capability map proposed')
  })

  it('names every initiative status', () => {
    for (const s of ['proposed', 'active', 'awaiting_human', 'blocked', 'done'] as const) {
      expect(INITIATIVE_STATUS_LABEL[s].length).toBeGreaterThan(0)
    }
    expect(initiativeStatusLabel('awaiting_human')).toBe('Awaiting human')
  })

  it('falls back to the raw value, and to a dash for nothing', () => {
    expect(stageLabel('bogus')).toBe('bogus')
    expect(stageLabel(null)).toBe('—')
    expect(onboardingStateLabel(undefined)).toBe('—')
  })

  it('never says project or feature to the user', () => {
    const all = [
      ...Object.values(PRODUCT), ...Object.values(CAPABILITY),
      ...Object.values(STAGE_LABEL), ...Object.values(INITIATIVE_STATUS_LABEL),
      ...Object.values(ONBOARDING_STATE_LABEL),
    ].join(' ').toLowerCase()
    expect(all).not.toMatch(/\bproject\b|\bfeature\b/)
  })
})
