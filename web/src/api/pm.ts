/**
 * PM product-layer API client — types + fetchers over `/api/pm` (phase 1).
 * Mirrors `api/sdlc.ts`: plain fetchers here, hooks in `api/hooks.ts`.
 * Exception: hooks that are tightly coupled to a single fetcher (e.g.
 * useProjectDeps) may live here to keep the interface co-located.
 */
import { useQuery } from '@tanstack/react-query'
import { api } from './client'
import type { MapChangesPointer } from './roadmap'

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

export interface OnboardingMapPr {
  /** The deterministic branch the map-synthesis session pushes to. */
  branch: string
  /** A GitHub PR search for that branch on the primary repo. */
  url: string
}

export interface OnboardingView {
  state: OnboardingState | string
  action: OnboardingAction
  counts: OnboardingCounts
  mapPr: OnboardingMapPr | null
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

/** Human gate: the capability-map PR is merged → map_merged. */
export function advanceMap(slug: string): Promise<{ onboardingState: string }> {
  return api.post<{ onboardingState: string }>(
    `/pm/projects/${encodeURIComponent(slug)}/advance-map`)
}

/** Human gate: the triage queue is drained → live. */
export function advanceLive(slug: string): Promise<{ onboardingState: string }> {
  return api.post<{ onboardingState: string }>(
    `/pm/projects/${encodeURIComponent(slug)}/advance-live`)
}

// ── Phase A: dependency mirror + drift audit ──────────────────────────────────

export interface DepEdge {
  src: string
  dst: string
  source: string
  active: boolean
  dstState: string | null
}

export interface DriftSummary {
  proseOnly: { src: string; dst: string }[]
  unresolvedTargets: { src: string; dst: string; source: string }[]
  missingFromProject: string[]
  checklistEpics: string[]
}

export interface ProjectDeps {
  project: { number: number | null; title: string | null; lastSyncedAt: string | null } | null
  edges: DepEdge[]
  drift: DriftSummary
}

export function fetchProjectDeps(slug: string): Promise<ProjectDeps> {
  return api.get<ProjectDeps>(`/pm/projects/${encodeURIComponent(slug)}/deps`)
}

export function useProjectDeps(slug: string, enabled = true) {
  return useQuery({
    queryKey: ['pm', 'deps', slug],
    queryFn: () => fetchProjectDeps(slug),
    enabled,
    staleTime: 5_000,
    refetchInterval: 15_000,
  })
}

export interface CreateEdgeInput { dstSlug: string; kind: EdgeKind; note?: string }

/** A product-level edge (`depends_on` / `deploys_via` / `integrates_with`), ViberPit-owned. */
export function createEdge(slug: string, input: CreateEdgeInput): Promise<ProjectEdgeInfo> {
  return api.post<ProjectEdgeInfo>(`/pm/projects/${encodeURIComponent(slug)}/edges`, input)
}

export function deleteEdge(slug: string, edgeId: string): Promise<void> {
  return api.del<void>(`/pm/projects/${encodeURIComponent(slug)}/edges/${encodeURIComponent(edgeId)}`)
}

/** Map-changes ops (spec §8/§9; server contract `server/api/pm.py:703` `MapChangeBody`,
 *  `Pointer.to_dict()`): each op below becomes exactly one commit on the product's
 *  open branch — the first opens the branch and PR, later ones append. */
export type MapChangeOp = 'create' | 'rename' | 'reparent' | 'summary' | 'links'

export interface MapChangeLink {
  kind: 'powered_by' | 'integrates_with'
  project: string
  feature: string
}

export interface MapChangeInput {
  op: MapChangeOp
  slug: string
  title?: string
  parentSlug?: string | null
  summary?: string
  links?: MapChangeLink[]
}

export interface MapChangeResult {
  pointer: MapChangesPointer
  path: string
  commitSha: string
}

export interface AbandonResult {
  pointer: MapChangesPointer
  cleanup: { deleted: number; orphaned: number }
}

/** One capability edit on the product's map-changes branch (R8c-1/R8c-2: a dialog
 *  sends one op per request, in sequence, for only the fields that changed). */
export function editMapChanges(slug: string, input: MapChangeInput): Promise<MapChangeResult> {
  return api.post<MapChangeResult>(`/pm/projects/${encodeURIComponent(slug)}/map-changes`, input)
}

/** R8c-4's one destructive roadmap action: closes the PR, deletes the branch, and
 *  drops every capability that only ever lived on it. */
export function abandonMapChanges(slug: string): Promise<AbandonResult> {
  return api.del<AbandonResult>(`/pm/projects/${encodeURIComponent(slug)}/map-changes`)
}

/** Merges the base branch into the map-changes PR through GitHub's own
 *  update-branch — only offered while the pointer reports `behind` (R8c-3's card). */
export function rebaseMapChanges(slug: string): Promise<{ pointer: MapChangesPointer }> {
  return api.post<{ pointer: MapChangesPointer }>(`/pm/projects/${encodeURIComponent(slug)}/map-changes/rebase`)
}

export interface RemoveDependencyInput { workItemId: string; blocker: string }
export interface RemoveDependencyResult { ok: boolean; src: string; blocker: string }

/** Delete the blocked-by edge `workItem ← blocker` on GitHub through the
 *  reconciler's journaled seam. A 502 carries `detail.step` (`node_id` means
 *  "not mirrored yet, wait for the sync"). */
export function removeDependency({ workItemId, blocker }: RemoveDependencyInput): Promise<RemoveDependencyResult> {
  return api.del<RemoveDependencyResult>(
    `/pm/work-items/${encodeURIComponent(workItemId)}/deps?blocker=${encodeURIComponent(blocker)}`)
}
