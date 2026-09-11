/**
 * SDLC Initiative API client — types + fetchers over `/api/sdlc` (issue #802/#803).
 *
 * Mirrors the `api/automations.ts` shape: plain fetchers here, React Query hooks
 * in `api/hooks.ts`. Field names are the server's camelCase JSON verbatim.
 */
import { api, apiErrorCode, ApiRequestError } from './client'

/** Lifecycle stages, in order — mirrors server INITIATIVE_STAGES. Single source
 *  of truth on the web side; the board renders the track from this. */
export const INITIATIVE_STAGES = [
  'intake', 'design', 'plan', 'build', 'deploy', 'maintain', 'done',
] as const
export type InitiativeStage = (typeof INITIATIVE_STAGES)[number]

/** The three committed artifact kinds, in chain order. */
export const INITIATIVE_ARTIFACT_KINDS = ['intent', 'spec', 'plan'] as const
export type ArtifactKind = (typeof INITIATIVE_ARTIFACT_KINDS)[number]

export type InitiativeStatus =
  | 'proposed' | 'active' | 'awaiting_human' | 'blocked' | 'done'

export interface InitiativeArtifact {
  id: string
  kind: ArtifactKind | string | null
  repoPath: string | null
  commitSha: string | null
  prNumber: number | null
  prUrl: string | null
  sessionId: string | null
  model: string | null
  createdAt: string | null
}

/** One child issue of an adopted epic (spec 003 §7c), by `WorkItem.parent_work_item_id`.
 *  Only the detail endpoint (`GET /initiatives/{slug}`) populates the `children`
 *  array this belongs to — see `InitiativeEpic` below. */
export interface InitiativeEpicChild {
  ref: string
  title: string | null
  state: string | null
  status: string | null
}

/** Sub-issue progress for an Initiative adopted from a GitHub-first epic
 *  (`epicRepo`/`epicNumber`). ccswitch deep-links to the GitHub epic via `url`
 *  rather than rebuilding a board (spec 003 §7c). Both `GET /initiatives` and
 *  `GET /initiatives/{slug}` compute and emit this object (or `null`) on every
 *  row — but the list endpoint omits `children`, since `InitiativeTrack` only
 *  ever reads `url`/`done`/`total`/`blocked`/`needsHuman` and that endpoint's
 *  query is re-run on every websocket status frame, so `children` is optional
 *  here rather than a second, list-only type. */
export interface InitiativeEpic {
  ref: string
  url: string
  total: number
  done: number
  blocked: number
  needsHuman: number
  children?: InitiativeEpicChild[]
}

/** A capability an initiative is linked to (`initiative_features`). */
export interface InitiativeCapabilityRef {
  id: string
  slug: string
  title: string | null
  isPrimary?: boolean
}

/** What the stage engine says should happen next (detail endpoint only). */
export interface InitiativeNextAction {
  type: 'propose' | 'await_approval' | 'materialize' | 'handoff_to_build' | 'done' | 'needs_human' | 'unknown' | string
  kind?: string
  issue?: string
  reason?: string
}

export interface Initiative {
  slug: string
  repo: string | null
  title: string | null
  stage: InitiativeStage | string | null
  status: InitiativeStatus | string | null
  origin: string | null
  originRef: string | null
  epicRepo: string | null
  epicNumber: number | null
  createdAt: string | null
  updatedAt: string | null
  artifacts: InitiativeArtifact[]
  // Both GET /initiatives and GET /initiatives/{slug} always emit this key —
  // null when the initiative has no epic pointer, an InitiativeEpic (with
  // `children` present only from the detail route) otherwise.
  epic: InitiativeEpic | null
  /** Phase 2 fields — every initiative endpoint emits them. */
  projectSlug: string | null
  primaryCapability: InitiativeCapabilityRef | null
  capabilities: InitiativeCapabilityRef[]
  /** Detail endpoint only. */
  buildSessions?: { id: string; status: string | null; branch: string | null }[]
  nextAction?: InitiativeNextAction
}

export interface CreateInitiativeInput {
  slug: string
  title: string
  repo: string          // required by the backend create body (`repo: str`)
  origin?: string
  originRef?: string
  primaryCapabilityId?: string
  capabilityIds?: string[]
}

export function fetchInitiative(slug: string): Promise<Initiative> {
  return api.get<Initiative>(`/sdlc/initiatives/${encodeURIComponent(slug)}`)
}

export function createInitiative(input: CreateInitiativeInput): Promise<Initiative> {
  return api.post<Initiative>('/sdlc/initiatives', input)
}

/** The human-gate tap: walk the initiative's stage forward one step. */
export function advanceInitiative(slug: string): Promise<Initiative> {
  return api.post<Initiative>(`/sdlc/initiatives/${encodeURIComponent(slug)}/advance`)
}

// ── Plan materialization: the decomposition gate (spec 003 §7b, #992) ────────

/** One task of a proposed decomposition.
 *
 *  `dependsOn` holds INDEXES into the same `tasks` array (the server's
 *  `depends_on`), not issue numbers: nothing exists on GitHub until the proposal
 *  is accepted, so there is nothing else a task could point at. */
export interface MaterializationTask {
  title: string | null
  body: string | null
  dependsOn: number[]
}

/** Proposal lifecycle — the same three the triage inbox uses (server
 *  `TRIAGE_PROPOSAL_STATUSES`). */
export type MaterializationStatus = 'pending' | 'accepted' | 'rejected'

/** One issue a partial apply already created, as the server records it on the
 *  proposal (`materialize_apply.create_issue`) — snake_case because it is the
 *  resume state, not a view model. */
export interface MaterializationCreatedIssue {
  node_id?: string
  number?: number | null
  url?: string | null
}

/** The apply's resume key: what already exists on GitHub for this proposal. The
 *  task map is keyed by the task's INDEX in `tasks` (as a string), and
 *  `repository_id` is a remembered constant, not an issue — counting it as one
 *  would tell a human an issue exists that does not. */
export interface MaterializationCreated {
  repository_id?: string
  epic?: MaterializationCreatedIssue | null
  tasks?: Record<string, MaterializationCreatedIssue> | null
}

/** How many issues a partial apply left behind — what a re-tap will NOT create
 *  again. Zero when nothing was created (or the apply never got past the
 *  repository lookup). */
export function createdIssueCount(created: MaterializationCreated | null | undefined): number {
  if (!created) return 0
  return (created.epic?.node_id ? 1 : 0) + Object.keys(created.tasks ?? {}).length
}

export interface MaterializationProposal {
  id: string
  initiativeId: string
  status: MaterializationStatus | string
  sessionId: string | null
  rationale: string | null
  epicTitle: string | null
  epicBody: string | null
  tasks: MaterializationTask[]
  /** What a partially applied accept already created on GitHub, so a re-tap
   *  reads as a resume rather than a second attempt. */
  created: MaterializationCreated | null
  createdAt: string | null
  resolvedAt: string | null
}

/** What the accept answers with once the epic, its sub-issues and the blocked-by
 *  chain exist (`server/pm/materialize_apply.py:apply`). */
export interface MaterializationApplied {
  ok: boolean
  epic: { node_id?: string; number?: number; url?: string } | null
  tasks: number
  edges: number
  /** Chain edges the apply could not create — a chain with a dropped edge looks
   *  identical on the board otherwise. */
  parkedEdges: number
  /** The stage the apply left the initiative in — `build`, because Materialize is
   *  the plan stage's terminal action and the confirmed accept IS its human gate
   *  (spec §7b). The detail query refetches after the mutation, so nothing here
   *  reads it; it is part of the response the audit trail is written from. */
  stage?: string
  syncRequested?: boolean
}

/**
 * The initiative's latest proposal, whatever its status, or `null` when it has
 * never been decomposed.
 *
 * One 404 is NOT a failure: most initiatives have no proposal, and the endpoint
 * says exactly that with `no_proposal`. The route answers 404 for TWO different
 * things, though — the other is `not_found`, meaning the initiative itself does
 * not exist — so the code is matched rather than the status. A missing
 * initiative must still reach the page as an error; rendering it as "nothing to
 * decide" would put a calm, empty Track tab on a dead address.
 *
 * `apiErrorCode` is the one decoder for that code (it already knows the server
 * spells it `detail.error` under FastAPI's envelope).
 */
export function fetchMaterialization(slug: string): Promise<MaterializationProposal | null> {
  return api
    .get<MaterializationProposal>(`/sdlc/initiatives/${encodeURIComponent(slug)}/materialization`)
    .catch((e: unknown) => {
      if (e instanceof ApiRequestError && e.status === 404 && apiErrorCode(e) === 'no_proposal') return null
      throw e
    })
}

/** The human gate: create the epic, its task sub-issues and the chain. A 502
 *  leaves the proposal `pending` and the apply resumable — the caller keeps the
 *  card up rather than clearing it. */
export function acceptMaterialization(slug: string): Promise<MaterializationApplied> {
  return api.post<MaterializationApplied>(
    `/sdlc/initiatives/${encodeURIComponent(slug)}/materialization/accept`)
}

/** Turn the decomposition down. The initiative stays where it is; the rejection
 *  only frees the synthesizer to decompose the plan again on a later tick. */
export function rejectMaterialization(slug: string): Promise<MaterializationProposal> {
  return api.post<MaterializationProposal>(
    `/sdlc/initiatives/${encodeURIComponent(slug)}/materialization/reject`)
}

// ── Control-band incidents: the SDLC Maintain-loop triage queue (#804) ───────
//
// NOT the PM triage inbox (`api/pm.ts`, `QK.triage`). That one holds AI proposals
// about how to classify work items; this one holds control-band INCIDENTS —
// deterministic metric breaches. The two share the word and nothing else, so
// everything here is named `incident`/`sdlcTriage` to keep them apart.

/** Lifecycle of an incident — mirrors server `TRIAGE_STATUSES`. */
export const INCIDENT_STATUSES = [
  'open', 'diagnosing', 'scheduled', 'queued', 'intake', 'dismissed',
] as const
export type IncidentStatus = (typeof INCIDENT_STATUSES)[number]

/** What a human may do to an incident — mirrors server `TRIAGE_ACTIONS`. */
export type IncidentAction = 'triage_now' | 'schedule' | 'dismiss'

export interface IncidentPoint {
  at: string
  value: number
}

export interface Incident {
  id: string
  /** Stable breach identity, `metric:rule:side`. */
  key: string | null
  metric: string | null
  source: string | null
  repo: string | null
  /** Western Electric rule that fired: we1 | we2 | we3 | we4. */
  rule: string | null
  sigma: number | null
  side: string | null
  /** bands.yaml tier this breach selected, and what that tier does. */
  tier: string | null
  response: string | null
  value: number | null
  baselineMean: number | null
  baselineStddev: number | null
  baselineN: number | null
  observedAt: string | null
  summary: string | null
  evidence: IncidentPoint[]
  status: IncidentStatus | string | null
  dismissedReason: string | null
  scheduledFor: string | null
  sessionId: string | null
  initiativeSlug: string | null
  occurrences: number | null
  createdAt: string | null
  updatedAt: string | null
}

export interface IncidentActionInput {
  id: string
  action: IncidentAction
  /** ISO-8601, required for `schedule` and rejected in the past. */
  scheduledFor?: string
  /** Required for `dismiss` — the queue is an audit trail. */
  reason?: string
}

export function fetchIncidents(): Promise<Incident[]> {
  return api
    .get<{ items: Incident[] }>('/sdlc/triage')
    .then((r) => r.items ?? [])
}

export function actOnIncident({ id, ...body }: IncidentActionInput): Promise<Incident> {
  return api.post<Incident>(`/sdlc/triage/${encodeURIComponent(id)}/action`, body)
}
