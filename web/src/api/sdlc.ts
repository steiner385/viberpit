/**
 * SDLC Initiative API client — types + fetchers over `/api/sdlc` (issue #802/#803).
 *
 * Mirrors the `api/automations.ts` shape: plain fetchers here, React Query hooks
 * in `api/hooks.ts`. Field names are the server's camelCase JSON verbatim.
 */
import { api } from './client'

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
}

export interface CreateInitiativeInput {
  slug: string
  title: string
  repo: string          // required by the backend create body (`repo: str`)
  origin?: string
  originRef?: string
}

export function fetchInitiatives(): Promise<Initiative[]> {
  return api.get<Initiative[]>('/sdlc/initiatives')
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
