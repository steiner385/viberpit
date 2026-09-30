/**
 * The one source of every user-facing word in the roadmap area (spec D11).
 *
 * Users see Product / Capability / Initiative / Epic / Issue. Code and the API
 * keep `project` and `feature`; this module is where the renaming happens, so
 * copy cannot drift between the nav, a chip, a button and an empty state.
 */
import { INITIATIVE_STAGES, type InitiativeStage, type InitiativeStatus } from '@/api/sdlc'
import type { OnboardingState } from '@/api/pm'
import type { InitiativeNode, RoadmapTier } from '@/api/roadmap'

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
  help: 'The product roadmap: every product, its capabilities, the initiatives changing them and the issues underneath. Drill down, trace back up, and see what needs you. Its Inbox scope holds what is awaiting your decision — proposed classifications to accept, adjust or reject, and product decisions a session drafted options for rather than guessing.',
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

/** Why an issue is held (server `holdKind`, spec 2026-09-18 §3). Evidence
 *  dispatch (item 4b) and the decision inbox (item 5) have landed, so those two
 *  labels name a surface a tap actually reaches. The model ladder (item 4a,
 *  #1961) has landed too, but `stronger_model` still has no tap target: it
 *  clears itself automatically on the next ladder dispatch, so its label stays
 *  neutral rather than naming a surface that doesn't exist for a human to press. */
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

/** The stages whose human gate is "read what the synthesizer wrote": their
 *  `STAGE_LABEL` names the artifact (Intent / Spec / Plan), so an
 *  `awaiting_human` row there asks for a review of exactly that. */
const ARTIFACT_STAGES: ReadonlySet<string> = new Set<InitiativeStage>(['intake', 'design', 'plan'])

/**
 * Why an initiative sits in the Needs-you queue and what the human DOES about
 * it — the row's subtitle and its CTA, as one pair so they cannot disagree.
 *
 * One verb per state rather than one "Review requested input" for all of them
 * (2026-09-30 Roadmap critique, P2: 165 rows ended in the same three words,
 * subtitled "Active"). Each `move` names the control the initiative page
 * answers with — `Advance → <stage>` is that page's button, "Clear the block"
 * is its Unblock / Decompose-again pair, "Review the spec" is the artifact the
 * synthesizer parked on — so the promise the queue makes is the button the tap
 * lands on. The map node carries no guidance object, so this is derived from
 * the same four facts the server used to place the row (`server/pm/tree.py:
 * initiative_tier`): reason, tier, status + stage, and the epic's held count.
 */
export function initiativeAsk(
  i: Pick<InitiativeNode, 'tier' | 'reason' | 'status' | 'stage' | 'epic'>,
): { why: string; move: string } {
  const why = reasonLabel(i.reason)
  if (i.reason === 'advance') {
    const next = nextStage(i.stage)
    return { why, move: next ? `Advance → ${stageLabel(next)}` : 'Complete' }
  }
  if (i.reason === 'epic_blocked') return { why, move: 'Clear the blocking dependencies' }
  if (i.tier === 'blocked') return { why: why || initiativeStatusLabel('blocked'), move: 'Clear the block' }
  if (i.status === 'awaiting_human' && i.stage && ARTIFACT_STAGES.has(i.stage)) {
    return { why: initiativeStatusLabel(i.status), move: `Review the ${stageLabel(i.stage).toLowerCase()}` }
  }
  const held = i.epic?.held ?? 0
  if (held > 0) {
    const noun = held === 1 ? ISSUE.singular.toLowerCase() : ISSUE.plural.toLowerCase()
    return { why: `${held} ${noun} held for you`, move: `Review ${held} held ${noun}` }
  }
  return { why: why || initiativeStatusLabel(i.status), move: 'Review and decide' }
}

/** The Delivery section and its list scopes (finish-line spec §4.1, §4.4). */
export const DELIVERY = {
  label: 'Delivery',
  automations: 'Automations',
  ci: 'CI health',
  capacity: 'Capacity',
  help: 'Is delivery healthy? Every automation that runs against your repos and sessions — what it is doing, what it needs from you, its schedule and configuration — and the CI health of each tracked repository: slow, flaky and regressing checks.',
  // Said in more than one place (the list column's empty state and the detail
  // pane's welcome pane; the error strings also in a header-level banner), so
  // they live here rather than as copies that can drift.
  emptyAutomations: 'No automations registered',
  emptyRepos: 'No repositories with pipeline history yet',
  automationsError: 'Could not load automations.',
  ciError: 'Could not load CI health.',
  capacityError: 'Could not load capacity.',
} as const

/** Tab labels shared by more than one page (`docs/PAGE-TABS.md` §3/§7): the
 *  same question reads the same on every object. Tier words come from
 *  `TIER_LABEL`; object words from `CAPABILITY`/`INITIATIVE`/`EPIC`. */
export const TAB_LABEL = {
  overview: 'Overview', ranked: 'Ranked', dependencies: 'Dependencies', automation: 'Automation',
  track: 'Track', phases: 'Phases', sessions: 'Sessions', queue: 'Queue', schedule: 'Schedule',
  config: 'Config', events: 'Events',
} as const

/** The automation detail's tabs, in order (`docs/PAGE-TABS.md` §3, §4: one
 *  tab, one question; configuration before history). `queue` is offered only
 *  to automations that have dispatchable work (`QUEUE_AUTOMATIONS` in
 *  `AutomationDetail.tsx`). `needs-you` is offered to every automation (the
 *  question exists for all of them; §4). */
export const AUTOMATION_TAB = {
  overview: TAB_LABEL.overview, 'needs-you': TIER_LABEL['needs-you'], queue: TAB_LABEL.queue,
  schedule: TAB_LABEL.schedule, config: TAB_LABEL.config, events: TAB_LABEL.events,
  sessions: TAB_LABEL.sessions,
} as const
export type AutomationTab = keyof typeof AUTOMATION_TAB
export const AUTOMATION_TABS = Object.keys(AUTOMATION_TAB) as readonly AutomationTab[]
