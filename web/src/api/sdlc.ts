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

export interface Initiative {
  slug: string
  repo: string | null
  title: string | null
  stage: InitiativeStage | string | null
  status: InitiativeStatus | string | null
  origin: string | null
  originRef: string | null
  createdAt: string | null
  updatedAt: string | null
  artifacts: InitiativeArtifact[]
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
