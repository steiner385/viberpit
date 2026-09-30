/**
 * PM triage API client — types + fetchers over `/api/pm/triage` (phase 2).
 * This is the CANONICAL triage module — phase 3 imports from here and upgrades
 * the hooks in place; it never forks a second TriageProposal/fetchTriage.
 */
import { api } from './client'
import type { Consequence } from './sdlc'

// Re-export the map/classify spawn fetchers so triage callers (and phase 3) have
// one import source for the whole triage surface.
export { proposeMap, runClassify as classify } from './pm'

export interface TriageProposal {
  /** Which KIND of inbox card this is. The server sends no discriminator — the
   *  inbox lists two endpoints' rows side by side (spec 2026-09-18 §6, "same
   *  ordering spec and keys") and `/pm/triage`'s own `kind` field means the WORK
   *  ITEM's kind (issue/pr), not the proposal's. So the tag is stamped at fetch
   *  time, here, by the one fetcher for each list; nothing downstream infers the
   *  kind from a field's presence. */
  proposalKind: 'triage'
  id: string
  workItemId: string | null
  /** Slug of the project the proposal's work item belongs to — the inbox header
   *  groups/labels by it when the list is not already scoped to one project. */
  projectSlug: string | null
  repo: string | null
  number: number | null
  kind: string | null
  title: string | null
  url: string | null
  proposedFeatureId: string | null
  proposedFeatureSlug: string | null
  noFitSuggestion: string | null
  reach: number | null
  impact: number | null
  effort: number | null
  confidence: number | null
  rationale: string | null
  confidenceLevel: string | null
  status: string
  createdAt: string | null
  /** The work item's `classified_by` once the proposal is resolved: `"human"`,
   *  `"auto"` (the PM Triage loop's auto-accept — spec §8), else null. */
  classifiedBy: string | null
  /** When the proposal was accepted/rejected; null while pending. */
  resolvedAt: string | null
  /** What Accept / Reject do under the owning product's dispatch policy
   *  (spec 2026-09-17 §4.3); absent from a server that predates it. */
  guidance?: { accept: Consequence; reject: Consequence } | null
}

/** One option a decision proposal offers. `detail` is the sentence under the
 *  label, when the proposer wrote one. */
export interface DecisionOption {
  key: string
  label: string
  detail?: string | null
}

/**
 * One pending DECISION proposal (`GET /api/pm/decisions`, spec 2026-09-18 §6):
 * a coding session hit a genuine product question, drafted the options instead
 * of guessing, and parked the item behind a `decision` hold until a person picks
 * one.
 *
 * Deliberately the SAME key names as `TriageProposal` wherever the two carry the
 * same fact (`workItemId`, `repo`, `number`, `title`, `url`, `projectSlug`,
 * `rationale`, `status`, `createdAt`, `guidance`) — the server's
 * `_decision_to_dict` spells them that way on purpose, so the inbox's one
 * ordering spec groups and sorts both kinds without a second accessor per field.
 */
export interface DecisionProposal {
  proposalKind: 'decision'
  id: string
  workItemId: string | null
  repo: string | null
  number: number | null
  /** `owner/repo#N` the proposal was made against, so a card can link out
   *  without re-joining `repo` and `number`. */
  ref: string | null
  projectSlug: string | null
  title: string | null
  url: string | null
  options: DecisionOption[]
  /** The option the proposer recommends — preselected in the card. */
  recommendedKey: string | null
  rationale: string | null
  chosenKey: string | null
  status: string
  /** Actor string of whoever proposed it (`session:<id>`, an identity, …). */
  proposedBy: string | null
  resolvedBy: string | null
  createdAt: string | null
  resolvedAt: string | null
  /** What Accept / Reject do under the owning product's dispatch policy — the
   *  same `Consequence` pair the classification card renders, through the same
   *  `ConsequenceList`. */
  guidance?: { accept: Consequence; reject: Consequence } | null
  /** Items this decision blocks, and how many of them dispatch the moment it is
   *  answered. Present only when the list was fetched with `impact`. */
  downstream?: number
  downstreamDispatchable?: number
}

/** A row of the Inbox queue, of either kind. `proposalKind` discriminates. */
export type InboxProposal = TriageProposal | DecisionProposal

export function isDecisionProposal(p: InboxProposal): p is DecisionProposal {
  return p.proposalKind === 'decision'
}

export function isTriageProposal(p: InboxProposal): p is TriageProposal {
  return p.proposalKind === 'triage'
}

/** Overlay fields a human may override when accepting (phase-3 edit-then-accept
 *  relies on this shape; phase 2 defines it). THE one overlay-edit type — the
 *  accept body and the overlay PATCH body are the same set of fields, so phase 3
 *  widened this in place rather than forking a second one. */
export interface OverlayEdits {
  reach?: number
  impact?: number
  effort?: number
  confidence?: number
  featureId?: string | null
  /** Inline-create a capability at accept time (spec 2026-09-22 §3) instead of
   *  homing on an existing one — mutually exclusive with `featureId` server-side. */
  newCapability?: { title: string; slug: string }
  triageState?: string
}

/** The persisted overlay row a PATCH returns (phase 3). */
export interface WorkItemOverlay {
  id: string
  repo: string | null
  number: number | null
  kind: string | null
  title: string | null
  state: string | null
  featureId: string | null
  reach: number | null
  impact: number | null
  effort: number | null
  confidence: number | null
  triageState: string | null
  classifiedBy: string | null
  classifiedAt: string | null
  url: string | null
}

/** Per-id outcome of a bulk sweep — partial success is normal, so the
 *  endpoints report each id rather than failing the whole batch. */
export interface BulkSweepRow { id: string; ok: boolean; error?: string }

export interface AcceptBulkResult {
  accepted: number
  results: BulkSweepRow[]
}

export interface RejectBulkResult {
  rejected: number
  results: BulkSweepRow[]
}

export function fetchTriage(projectSlug?: string): Promise<TriageProposal[]> {
  const qs = projectSlug
    ? `?projectSlug=${encodeURIComponent(projectSlug)}`
    : ''
  return api.get<Omit<TriageProposal, 'proposalKind'>[]>(`/pm/triage${qs}`)
    .then((rows) => rows.map((r) => ({ ...r, proposalKind: 'triage' as const })))
}

/**
 * Pending decision proposals — the inbox's second card kind.
 *
 * Scoped server-side (spec 2026-09-30 §4.3): `projectSlug` narrows like
 * `/pm/triage`; `impact` adds `downstream` / `downstreamDispatchable` from the
 * escalation read model — opt-in because the nav badge polls this list every
 * 15 s and the impact costs the queue read.
 */
export function fetchDecisions(
  projectSlug?: string,
  { impact = false }: { impact?: boolean } = {},
): Promise<DecisionProposal[]> {
  const qs = new URLSearchParams()
  if (projectSlug) qs.set('projectSlug', projectSlug)
  if (impact) qs.set('impact', 'true')
  const q = qs.toString()
  const suffix = q ? `?${q}` : ''
  return api.get<Omit<DecisionProposal, 'proposalKind'>[]>(`/pm/decisions${suffix}`)
    .then((rows) => rows.map((r) => ({ ...r, proposalKind: 'decision' as const })))
}

/** What accept/reject answer: the resolved row. Only the fields a caller acts
 *  on are named — the row is leaving the queue either way, so nothing re-renders
 *  from it and it is NOT tagged with a `proposalKind` it would never be read
 *  through. */
export interface DecisionResult {
  id: string
  status: string
  chosenKey: string | null
}

/** Take the decision: `chosenKey` is the option the person picked. */
export function acceptDecision(id: string, chosenKey: string): Promise<DecisionResult> {
  return api.post<DecisionResult>(`/pm/decisions/${encodeURIComponent(id)}/accept`,
    { chosen_key: chosenKey })
}

/** Refuse the options — the item becomes the caller's owner-action hold. */
export function rejectDecision(id: string): Promise<DecisionResult> {
  return api.post<DecisionResult>(`/pm/decisions/${encodeURIComponent(id)}/reject`)
}

export function acceptProposal(
  id: string, edits?: OverlayEdits,
): Promise<{ id: string; status: string; workItemId: string | null; triageState: string }> {
  return api.post(`/pm/triage/${encodeURIComponent(id)}/accept`,
    edits ? { edits } : undefined)
}

export function rejectProposal(id: string): Promise<{ id: string; status: string }> {
  return api.post(`/pm/triage/${encodeURIComponent(id)}/reject`)
}

export interface UndoResult { id: string; status: 'pending'; workItemId: string | null; triageState: string | null }

/** Put an accepted/rejected proposal back to pending (server window ≈ 60 s). */
export function undoProposal(id: string): Promise<UndoResult> {
  return api.post<UndoResult>(`/pm/triage/${encodeURIComponent(id)}/undo`)
}

/** Accept many proposals in one round trip (the inbox's `A` / accept-selected). */
export function acceptBulk(ids: string[]): Promise<AcceptBulkResult> {
  return api.post<AcceptBulkResult>('/pm/triage/accept-bulk', { ids })
}

/** Reject many proposals in one round trip (the inbox's `R` / reject-selected). */
export function rejectBulk(ids: string[]): Promise<RejectBulkResult> {
  return api.post<RejectBulkResult>('/pm/triage/reject-bulk', { ids })
}

/** Edit a work item's overlay directly (inline score edits, triage-state moves). */
export function updateOverlay(
  itemId: string, edit: OverlayEdits,
): Promise<WorkItemOverlay> {
  return api.patch<WorkItemOverlay>(
    `/pm/work-items/${encodeURIComponent(itemId)}/overlay`, edit)
}
