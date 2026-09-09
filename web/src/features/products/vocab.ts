/**
 * The one source of every user-facing word in the roadmap area (spec D11).
 *
 * Users see Product / Capability / Initiative / Epic / Issue. Code and the API
 * keep `project` and `feature`; this module is where the renaming happens, so
 * copy cannot drift between the nav, a chip, a button and an empty state.
 */
import type { InitiativeStage, InitiativeStatus } from '@/api/sdlc'
import type { OnboardingState } from '@/api/pm'
import type { RoadmapTier } from '@/api/roadmap'

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

/** The Roadmap section and its list scopes (finish-line spec §4.1, §4.3). */
export const ROADMAP = {
  label: 'Roadmap',
  map: 'Map',
  initiatives: 'Initiatives',
  queue: 'Queue',
  inbox: 'Inbox',
  help: 'The product roadmap: every product, its capabilities, the initiatives changing them and the issues underneath. Drill down, trace back up, and see what needs you.',
} as const

export const INITIATIVE = { singular: 'Initiative', plural: 'Initiatives', new: 'New initiative' } as const
export const ISSUE = { singular: 'Issue', plural: 'Issues' } as const
export const EPIC = { singular: 'Epic' } as const

/** One attention tier per row (spec §11); the words the pill and Needs-you tab use. */
export const TIER_LABEL: Record<RoadmapTier, string> = {
  'needs-you': 'Needs you',
  blocked: 'Blocked',
  'in-flight': 'In flight',
  proposed: 'Proposed',
  live: 'Live',
  idle: 'Idle',
}

/** Why a row needs a human beyond its tier (server `reason`). */
export const REASON_LABEL: Record<string, string> = {
  advance: 'Ready to advance',
}

export const reasonLabel = (reason: string | null | undefined): string =>
  reason ? (REASON_LABEL[reason] ?? reason) : ''
