// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { INITIATIVE_STAGES } from '@/api/sdlc'
import { PROJECT_ONBOARDING_STATES } from '@/api/pm'
import {
  PRODUCT, CAPABILITY, STAGE_LABEL, stageLabel,
  INITIATIVE_STATUS_LABEL, initiativeStatusLabel,
  ONBOARDING_STATE_LABEL, onboardingStateLabel,
  CAPABILITY_STATE_LABEL, REASON_LABEL, ROADMAP, TIER_LABEL,
  nextStage, reasonLabel,
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

  // EVERY exported table, not a sample of them: the guard is only worth having
  // if adding a table to `vocab.ts` cannot quietly escape it.
  it('never says project or feature to the user', () => {
    const all = [
      ...Object.values(PRODUCT), ...Object.values(CAPABILITY),
      ...Object.values(STAGE_LABEL), ...Object.values(INITIATIVE_STATUS_LABEL),
      ...Object.values(ONBOARDING_STATE_LABEL), ...Object.values(CAPABILITY_STATE_LABEL),
      ...Object.values(TIER_LABEL), ...Object.values(ROADMAP),
      ...Object.values(REASON_LABEL),
    ].join(' ').toLowerCase()
    expect(all).not.toMatch(/\bproject\b|\bfeature\b/)
  })
})

it('names the roadmap surfaces and every tier', () => {
  expect(ROADMAP.label).toBe('Roadmap')
  expect(Object.keys(TIER_LABEL)).toEqual(['needs-you', 'blocked', 'in-flight', 'proposed', 'live', 'idle'])
  expect(reasonLabel('advance')).toBe('Ready to advance')
  expect(reasonLabel(null)).toBe('')
})

it('names the stage the gate would move to, and nothing past the end', () => {
  expect(nextStage('build')).toBe('deploy')
  expect(nextStage('done')).toBeNull()
  // An unknown or absent stage has no next: the gate cannot name a target.
  expect(nextStage('bogus')).toBeNull()
  expect(nextStage(null)).toBeNull()
})

describe('holdLabel', () => {
  it('names every hold kind without promising a later series item', async () => {
    const { HOLD_LABEL, holdLabel } = await import('./vocab')
    expect(Object.keys(HOLD_LABEL).sort()).toEqual(['decision', 'evidence', 'owner_action', 'stronger_model'])
    expect(holdLabel('owner_action')).toBe('Held for the owner')
    expect(holdLabel('evidence')).toBe('Held for evidence')
    expect(holdLabel(null)).toBe('')
    for (const v of Object.values(HOLD_LABEL)) expect(v).not.toMatch(/investigat|next run|tier/i)
  })
})

describe('DELIVERY vocabulary (finish-line D9)', () => {
  it('names the section, both scopes and the six automation tabs', async () => {
    const { DELIVERY, AUTOMATION_TAB } = await import('./vocab')
    expect(DELIVERY.label).toBe('Delivery')
    expect([DELIVERY.automations, DELIVERY.ci]).toEqual(['Automations', 'CI health'])
    expect(Object.values(AUTOMATION_TAB)).toEqual(['Overview', 'Queue', 'Schedule', 'Config', 'Events', 'Sessions'])
    expect(DELIVERY.help.length).toBeGreaterThan(40)
  })

  // Each said in more than one component (list empty state, welcome pane,
  // error banner); pinned here so a future edit cannot fix the copy in one
  // place and leave the other stale (finding #3, final review).
  it('says each duplicated empty/error sentence once', async () => {
    const { DELIVERY } = await import('./vocab')
    expect(DELIVERY.emptyAutomations).toBe('No automations registered')
    expect(DELIVERY.emptyRepos).toBe('No repositories with pipeline history yet')
    expect(DELIVERY.automationsError).toBe('Could not load automations.')
    expect(DELIVERY.ciError).toBe('Could not load CI health.')
  })
})
