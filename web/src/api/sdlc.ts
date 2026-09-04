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

/** One child issue of an adopted epic (spec 003 §7c), by `WorkItem.parent_work_item_id`. */
export interface InitiativeEpicChild {
  ref: string
  title: string | null
  state: string | null
  status: string | null
}

/** Sub-issue progress for an Initiative adopted from a GitHub-first epic
 *  (`epicRepo`/`epicNumber`). ccswitch deep-links to the GitHub epic via `url`
 *  rather than rebuilding a board (spec 003 §7c). */
export interface InitiativeEpic {
  ref: string
  url: string
  total: number
  done: number
  blocked: number
  needsHuman: number
  children: InitiativeEpicChild[]
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
  // Only the detail endpoint (GET /initiatives/{slug}) computes this; the list
  // endpoint omits the key entirely, so it's optional here (not just nullable).
  epic?: InitiativeEpic | null
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
