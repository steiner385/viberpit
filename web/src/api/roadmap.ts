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
  /** Derived priority (server/pm/priority.py): the epic's RICE score, else the
   *  highest open sub-issue score, else null. Never stored, never recomputed here. */
  priority: number | null
  /** What the synthesizer last did with one of this row's gates under the
   *  product's risk tolerance, only inside the 24 h chip window (server
   *  `sdlc/gates.py:auto_resolved_view`), else null. */
  autoResolved: AutoResolved | null
  /** Slug of the program initiative this is a phase of, or null for a root
   *  initiative (spec: initiative nesting). A phase whose parent names an
   *  initiative not present in the product's tree is treated as a root — the
   *  web never hides a node over a dangling reference. */
  parentInitiative: string | null
  /** Whether this initiative has phases nested under it (a "program"). */
  hasChildren: boolean
}

export interface AutoResolved {
  gate: string
  /** e.g. "advanced to plan", "merged spec", "auto-merge unavailable" */
  summary: string
  /** The existing human reverse action ("Step back", ...), or null. */
  reverse: string | null
  at: string
  outcome: 'resolved' | 'refused'
  tolerance: string | null
}

export interface ProductCounts {
  inFlight: number
  blocked: number
  needsYou: number
  capabilities: number
  awaitingTriage: number
  needsHome: number
}

/**
 * The product's ViberPit-owned map-changes branch/PR (spec §9,
 * `server/pm/map_changes.py:Pointer`). Capability edits from the map commit to
 * it and show as proposed until it merges; `behind` is what offers Rebase and
 * `conflicting` what raises the conflict banner.
 */
export interface MapChangesPointer {
  branch: string
  prNumber: number | null
  prUrl: string | null
  status: 'open' | 'merged' | 'abandoned' | string
  openedAt: string | null
  headSha: string | null
  behind: boolean
  conflicting: boolean
}

export interface ProductNode {
  slug: string
  name: string | null
  tier: RoadmapTier
  counts: ProductCounts
  capabilities: CapabilityNode[]
  /** Every initiative of the product, flat; nesting is the web's job. */
  initiatives: InitiativeNode[]
  /** The OPEN map-changes PR, or null when the product has none. */
  mapChanges: MapChangesPointer | null
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
  /** The capability the issue sits under on the map; null while unclassified. */
  featureId: string | null
  sessionStatus: string | null
  ghCreatedAt: string | null
  /** Why this leaf needs a human beyond its tier (product automation policy
   *  §7), or null. `'needs_spec'` is the only value today — the body has
   *  bounced `bounces` times at its current fingerprint without merging. */
  reason: string | null
  /** Finalised, non-merged dispatches at the CURRENT body fingerprint (D8);
   *  0 when `reason` is null. */
  bounces: number
  /** Why this leaf is held beyond its tier (hold ladder, spec 2026-09-18 §3),
   *  or null when it is not held. Rendered through `holdLabel`. */
  holdKind: string | null
  /** The runner's own words for why it gave up, or null. */
  holdReason: string | null
  /** Who or what set the hold — an identity or a subsystem name — or null. */
  holdBy: string | null
  /** When the hold was set, or null when there is none. */
  holdSetAt: string | null
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
