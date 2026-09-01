/**
 * PM product-layer API client — types + fetchers over `/api/pm` (phase 1).
 * Mirrors `api/sdlc.ts`: plain fetchers here, hooks in `api/hooks.ts`.
 */
import { api } from './client'

export const PROJECT_ONBOARDING_STATES = [
  'registered', 'syncing', 'map_proposed', 'map_merged', 'classifying', 'live',
] as const
export type OnboardingState = (typeof PROJECT_ONBOARDING_STATES)[number]

export const PROJECT_EDGE_KINDS = ['depends_on', 'deploys_via', 'integrates_with'] as const
export type EdgeKind = (typeof PROJECT_EDGE_KINDS)[number]

export interface ProjectRepoInfo {
  id: string
  repo: string | null
  isPrimary: boolean
  backfillDone: boolean
  lastSyncedAt: string | null
}

export interface ProjectEdgeInfo {
  id: string
  srcSlug: string | null
  dstSlug: string | null
  kind: EdgeKind | string | null
  note: string | null
}

export interface Project {
  id: string
  slug: string | null
  name: string | null
  description: string | null
  status: string | null
  portfolioTags: string[]
  onboardingState: OnboardingState | string | null
  createdAt: string | null
  updatedAt: string | null
  repos: ProjectRepoInfo[]
  edges: ProjectEdgeInfo[]
  featureCount: number
  workItemCount: number
}

export interface CreateProjectInput {
  name: string
  slug?: string
  description?: string
  repos: { repo: string; isPrimary?: boolean }[]
}

export function fetchProjects(): Promise<Project[]> {
  return api.get<Project[]>('/pm/projects')
}

export function createProject(input: CreateProjectInput): Promise<Project> {
  return api.post<Project>('/pm/projects', input)
}

export function requestProjectSync(slug: string): Promise<{ requested: boolean }> {
  return api.post<{ requested: boolean }>(`/pm/projects/${encodeURIComponent(slug)}/sync`)
}

// --- phase 2: onboarding wizard (design §5) ---

export interface OnboardingAction {
  type:
    | 'register' | 'await_backfill' | 'propose_map' | 'await_map_pr'
    | 'classify' | 'await_triage' | 'live' | 'needs_human' | 'unknown'
  reason?: string
}

export interface OnboardingCounts {
  repoCount: number
  backfillDoneCount: number
  featureCount: number
  openUnclassifiedCount: number
  pendingProposalCount: number
}

export interface OnboardingView {
  state: OnboardingState | string
  action: OnboardingAction
  counts: OnboardingCounts
}

export function fetchOnboarding(slug: string): Promise<OnboardingView> {
  return api.get<OnboardingView>(
    `/pm/projects/${encodeURIComponent(slug)}/onboarding`)
}

export function proposeMap(slug: string): Promise<{ sessionId: string | null }> {
  return api.post<{ sessionId: string | null }>(
    `/pm/projects/${encodeURIComponent(slug)}/propose-map`)
}

// Also exported as `classify` from the triage module (single spawn fetcher).
export function runClassify(slug: string): Promise<{ dispatched: number }> {
  return api.post<{ dispatched: number }>(
    `/pm/projects/${encodeURIComponent(slug)}/classify`)
}
