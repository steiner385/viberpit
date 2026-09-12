/**
 * Roadmap tree API client (spec 2026-09-07 §10): the map every product renders
 * from, the issue leaves under one node, the ranked queue, and one project.
 * Plain fetchers here, React Query hooks in `api/hooks.ts`. Field names are the
 * server's camelCase JSON verbatim (`server/pm/tree.py`, `server/api/pm_tree.py`).
 */
import { api } from './client'
import type { InitiativeEpic } from './sdlc'
import type { Project } from './pm'

/** Attention tiers, highest first — mirrors `server/pm/tree.py:TIERS`. */
export const ROADMAP_TIERS = ['needs-you', 'blocked', 'in-flight', 'proposed', 'live', 'idle'] as const
export type RoadmapTier = (typeof ROADMAP_TIERS)[number]

export interface CapabilityCounts {
  openIssues: number
  inFlight: number
  blocked: number
  needsYou: number
}

export interface CapabilityLink {
  kind: 'powered_by' | 'integrates_with' | string
  projectSlug: string
  featureSlug: string
}

export interface CapabilityNode {
  id: string
  slug: string
  title: string | null
  state: string | null
  summary: string | null
  docPath: string | null
  commitSha: string | null
  prNumber: number | null
  parentSlug: string | null
  tier: RoadmapTier
  counts: CapabilityCounts
  links: CapabilityLink[]
  /** Slugs of initiatives that TOUCH this capability (not primary). */
  initiatives: string[]
  children: CapabilityNode[]
}

export interface InitiativeNode {
  slug: string
  title: string | null
  stage: string | null
  status: string | null
  projectSlug: string
  /** Primary capability SLUG (the detail endpoint returns an object instead). */
  primaryCapability: string | null
  capabilities: string[]
  tier: RoadmapTier
  /** Why it needs a human beyond its status: 'advance', or for a blocked row 'blocked' | 'epic_blocked'. */
  reason: 'advance' | 'blocked' | 'epic_blocked' | string | null
  epic: InitiativeEpic | null
}

export interface ProductCounts {
  inFlight: number
  blocked: number
  needsYou: number
  capabilities: number
  awaitingTriage: number
  needsHome: number
}

export interface ProductNode {
  slug: string
  name: string | null
  tier: RoadmapTier
  counts: ProductCounts
  capabilities: CapabilityNode[]
  /** Every initiative of the product, flat; nesting is the web's job. */
  initiatives: InitiativeNode[]
}

export interface RoadmapMap {
  products: ProductNode[]
  generatedAt: string
}

export interface Leaf {
  id: string
  ref: string
  repo: string
  number: number
  kind: 'issue' | 'pr' | string
  title: string | null
  url: string | null
  state: string | null
  triageState: string | null
  score: number | null
  reach: number | null
  impact: number | null
  effort: number | null
  confidence: number | null
  tier: RoadmapTier
  blockedBy: string[]
  parentRef: string | null
  sessionStatus: string | null
  ghCreatedAt: string | null
}

/** Exactly one of the two, enforced by the server (422 `bad_scope`). */
export type LeavesScope = { capability: string; initiative?: never } | { initiative: string; capability?: never }

export interface QueueEntry {
  ref: string
  repo: string
  number: number
  product: string | null
  title: string | null
  url: string | null
  score: number | null
}

export interface QueueView {
  entries: QueueEntry[]
  skipped: unknown
  generatedAt: string
}

export function fetchRoadmapMap(): Promise<RoadmapMap> {
  return api.get<RoadmapMap>('/pm/map')
}

export async function fetchLeaves(scope: LeavesScope): Promise<Leaf[]> {
  const q = scope.capability !== undefined
    ? `capability=${encodeURIComponent(scope.capability)}`
    : `initiative=${encodeURIComponent(scope.initiative)}`
  const res = await api.get<{ items: Leaf[] }>(`/pm/leaves?${q}`)
  return res.items
}

export function fetchQueue(): Promise<QueueView> {
  return api.get<QueueView>('/pm/queue')
}

export function fetchProject(slug: string): Promise<Project> {
  return api.get<Project>(`/pm/projects/${encodeURIComponent(slug)}`)
}
