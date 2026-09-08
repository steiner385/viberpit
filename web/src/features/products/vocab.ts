/**
 * The one source of every user-facing word in the roadmap area (spec D11).
 *
 * Users see Product / Capability / Initiative / Epic / Issue. Code and the API
 * keep `project` and `feature`; this module is where the renaming happens, so
 * copy cannot drift between the nav, a chip, a button and an empty state.
 */
import type { InitiativeStage, InitiativeStatus } from '@/api/sdlc'
import type { OnboardingState } from '@/api/pm'

export const PRODUCT = {
  singular: 'Product',
  plural: 'Products',
  new: 'New product',
  empty: 'No products yet. Add one to start mirroring its backlog.',
} as const

export const CAPABILITY = {
  singular: 'Capability',
  plural: 'Capabilities',
} as const

/** The first three stages surface the artifact they commit, so the track reads
 *  as the playbook's intent → spec → plan → build progression. */
export const STAGE_LABEL: Record<InitiativeStage, string> = {
  intake: 'Intent',
  design: 'Spec',
  plan: 'Plan',
  build: 'Build',
  deploy: 'Deploy',
  maintain: 'Maintain',
  done: 'Done',
}

export const INITIATIVE_STATUS_LABEL: Record<InitiativeStatus, string> = {
  proposed: 'Proposed',
  active: 'Active',
  awaiting_human: 'Awaiting human',
  blocked: 'Blocked',
  done: 'Done',
}

export const ONBOARDING_STATE_LABEL: Record<OnboardingState, string> = {
  registered: 'Registered',
  syncing: 'Syncing backlog',
  map_proposed: 'Capability map proposed',
  map_merged: 'Capability map merged',
  classifying: 'Classifying backlog',
  live: 'Live',
}

function lookup(table: Record<string, string>, key: string | null | undefined): string {
  if (key == null || key === '') return '—'
  return table[key] ?? key
}

export const stageLabel = (stage: string | null | undefined) => lookup(STAGE_LABEL, stage)
export const initiativeStatusLabel = (status: string | null | undefined) =>
  lookup(INITIATIVE_STATUS_LABEL, status)
export const onboardingStateLabel = (state: string | null | undefined) =>
  lookup(ONBOARDING_STATE_LABEL, state)
