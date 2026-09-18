/**
 * The one source of every user-facing word in the roadmap area (spec D11).
 *
 * Users see Product / Capability / Initiative / Epic / Issue. Code and the API
 * keep `project` and `feature`; this module is where the renaming happens, so
 * copy cannot drift between the nav, a chip, a button and an empty state.
 */
import { INITIATIVE_STAGES, type InitiativeStage, type InitiativeStatus } from '@/api/sdlc'
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

/** Where a capability is in its life, as its feature doc declares it
 *  (`FEATURE_LIFECYCLE_STATES` in `server/db/models.py`). Not a tier: this
 *  is the intent a human wrote down, the tier is what the work is doing today. */
export const CAPABILITY_STATE_LABEL: Record<string, string> = {
  proposed: 'Proposed',
  discovery: 'Discovery',
  delivering: 'Delivering',
  live: 'Live',
  sunset: 'Sunset',
}

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
export const capabilityStateLabel = (state: string | null | undefined) =>
  lookup(CAPABILITY_STATE_LABEL, state)

/** The Roadmap section and its list scopes (finish-line spec §4.1, §4.3). */
export const ROADMAP = {
  label: 'Roadmap',
  map: 'Map',
  initiatives: 'Initiatives',
  queue: 'Queue',
  inbox: 'Inbox',
  help: 'The product roadmap: every product, its capabilities, the initiatives changing them and the issues underneath. Drill down, trace back up, and see what needs you. Its Inbox scope holds the proposed classifications awaiting your decision — accept, adjust or reject each one.',
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
  // Cause-neutral on purpose: `blocked` has two writers (a failed materialize
  // apply and the synthesizer giving up on a plan), and the row does not say which.
  blocked: 'Blocked, needs clearing',
  epic_blocked: 'Blocked by open dependencies',
  // A leaf's reason, not an initiative's — `bouncedLabel` below says the SAME
  // thing with the bounce count, which is what the leaf chip actually renders.
  needs_spec: 'Needs spec',
}

export const reasonLabel = (reason: string | null | undefined): string =>
  reason ? (REASON_LABEL[reason] ?? reason) : ''

/** Why an issue is held (server `holdKind`, spec 2026-09-18 §3). Neutral on
 *  purpose: evidence dispatch has landed (item 4b), but the stronger-model exit
 *  and the decision inbox are still later series items, and a label must never
 *  promise those before they exist. */
export const HOLD_LABEL: Record<string, string> = {
  owner_action: 'Held for the owner',
  decision: 'Awaiting your decision',
  evidence: 'Held for evidence',
  stronger_model: 'Held for a stronger model',
}

export const holdLabel = (kind: string | null | undefined): string =>
  kind ? (HOLD_LABEL[kind] ?? kind) : ''

/** The leaf chip's text for `reason === 'needs_spec'` (product automation
 *  policy §7): the issue's body has bounced `n` times at its current
 *  fingerprint without merging, so the runner has stopped re-dispatching it
 *  until a human edits it. */
export const bouncedLabel = (n: number) => `bounced ${n}× — needs spec`

/** The stage the human gate would move an initiative to, or null at the end of
 *  the lifecycle. Beside `reasonLabel` because the two are said together: a row
 *  that is "Ready to advance" is ready to advance TO something, and the header
 *  button on the initiative page names the same stage. */
export function nextStage(stage: string | null | undefined): InitiativeStage | null {
  const i = INITIATIVE_STAGES.indexOf(stage as InitiativeStage)
  return i < 0 || i + 1 >= INITIATIVE_STAGES.length ? null : INITIATIVE_STAGES[i + 1]!
}

/** The stage the step-back tap would return an initiative to, or null at the first stage. */
export function prevStage(stage: string | null | undefined): InitiativeStage | null {
  const i = INITIATIVE_STAGES.indexOf(stage as InitiativeStage)
  return i <= 0 ? null : INITIATIVE_STAGES[i - 1]!
}

/** The Delivery section and its list scopes (finish-line spec §4.1, §4.4). */
export const DELIVERY = {
  label: 'Delivery',
  automations: 'Automations',
  ci: 'CI health',
  help: 'Is delivery healthy? Every automation that runs against your repos and sessions — what it is doing, what it needs from you, its schedule and configuration — and the CI health of each tracked repository: slow, flaky and regressing checks.',
  // Said in more than one place (the list column's empty state and the detail
  // pane's welcome pane; the error strings also in a header-level banner), so
  // they live here rather than as copies that can drift.
  emptyAutomations: 'No automations registered',
  emptyRepos: 'No repositories with pipeline history yet',
  automationsError: 'Could not load automations.',
  ciError: 'Could not load CI health.',
} as const

/** The automation detail's tabs, in order (finish-line spec §4.4). */
export const AUTOMATION_TAB = {
  status: 'Status', schedule: 'Schedule', events: 'Events', sessions: 'Sessions', config: 'Config',
} as const
export type AutomationTab = keyof typeof AUTOMATION_TAB
export const AUTOMATION_TABS = Object.keys(AUTOMATION_TAB) as readonly AutomationTab[]
