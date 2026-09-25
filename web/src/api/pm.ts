/**
 * PM product-layer API client — types + fetchers over `/api/pm` (phase 1).
 * Mirrors `api/sdlc.ts`: plain fetchers here, hooks in `api/hooks.ts`.
 * Exception: hooks that are tightly coupled to a single fetcher (e.g.
 * useProjectDeps) may live here to keep the interface co-located.
 */
import { useQuery } from '@tanstack/react-query'
import { api } from './client'
import type { ConfigField } from './automations'
import type { MapChangesPointer } from './roadmap'
import type { Consequence } from './sdlc'

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
  /** The RAW stored automation-policy blob (server/automations/product_policy.py),
   *  or null when the product has never opted in. Render it through
   *  `PRODUCT_POLICY_SCHEMA`'s defaults (`usePolicySchema`) — never trust this
   *  alone as "every key present". */
  automationPolicy: Record<string, unknown> | null
  automationPolicyUpdatedAt: string | null
  /** Capacity governor (spec 2026-09-19 §4): 1 = highest priority; null = unranked. */
  priorityRank: number | null
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

/** Sparse patch: only the fields present are changed. `repo` repoints the
 *  PRIMARY repo in place — not an add/remove-multiple-repos edit. */
export interface UpdateProjectInput {
  name?: string
  slug?: string
  repo?: string
  priorityRank?: number | null
}

export function updateProject(slug: string, input: UpdateProjectInput): Promise<Project> {
  return api.patch<Project>(`/pm/projects/${encodeURIComponent(slug)}`, input)
}

export function requestProjectSync(slug: string): Promise<{ requested: boolean }> {
  return api.post<{ requested: boolean }>(`/pm/projects/${encodeURIComponent(slug)}/sync`)
}

// --- product automation policy (design §4/§9; server contract server/api/pm.py) ---

/** The ONE schema every product's automation policy validates against
 *  (server `PRODUCT_POLICY_SCHEMA`) — `staleTime: Infinity` in `usePolicySchema`
 *  since it only changes on a deploy. */
export function fetchPolicySchema(): Promise<ConfigField[]> {
  return api.get<ConfigField[]>('/pm/policy-schema')
}

/** Read-only registration evidence, not an automation authorization decision. */
export interface ProductPolicyCoverage {
  projectSlug: string
  observedAt: string
  scope: 'repository-coverage'
  explanation: string
  repositories: {
    requestedRepo: string
    canonicalRepo: string | null
    state: 'unique' | 'ambiguous' | 'missing' | 'unresolved'
    owner: { id: string; slug: string } | null
    candidates: { id: string; slug: string }[]
    unresolvedRepos: string[]
    reason: string
  }[]
}

export function fetchProductPolicyCoverage(slug: string): Promise<ProductPolicyCoverage> {
  return api.get<ProductPolicyCoverage>(`/pm/projects/${encodeURIComponent(slug)}/policy/coverage`)
}

/** Current policy eligibility, never a cached authorization to launch a session. */
export interface ProductImplementationDecision {
  state: 'permitted' | 'refused' | 'unknown'
  constraints: { code: string; state: 'permitted' | 'refused' | 'unknown'; text: string; link: string | null }[]
  configuredDispatch: string
  policySource: string
  productId: string | null
  productSlug: string | null
  canonicalRepo: string | null
  approvalRequired: boolean
  observedAt: string
  policyRevision: string
  remainingChecks: string[]
  note: string
}

export interface ProductImplementationPolicy {
  projectSlug: string
  observedAt: string
  scope: 'implementation-policy-eligibility'
  explanation: string
  repositories: {
    requestedRepo: string
    canonicalRepo: string | null
    coverageState: ProductPolicyCoverage['repositories'][number]['state']
    decisions: {
      automaticImplementation: ProductImplementationDecision
      automaticProposal: ProductImplementationDecision
      manualImplementation: ProductImplementationDecision
    }
  }[]
}

export function fetchProductImplementationPolicy(slug: string): Promise<ProductImplementationPolicy> {
  return api.get<ProductImplementationPolicy>(`/pm/projects/${encodeURIComponent(slug)}/policy/effective`)
}

/** Sparse validated edit. Null deliberately resets; the exact observed revision
 * guards stale clients, including a null revision for a never-edited product. */
export interface ProductPolicyPatch {
  policy: Record<string, unknown> | null
  expectedUpdatedAt?: string | null
}

export function updateProductPolicy(slug: string, patch: ProductPolicyPatch): Promise<Project> {
  return api.patch<Project>(`/pm/projects/${encodeURIComponent(slug)}/policy`, patch)
}

// --- phase 2: onboarding wizard (design §5) ---

export interface OnboardingAction {
  type:
    | 'register' | 'await_backfill' | 'propose_map' | 'await_map_pr'
    | 'classify' | 'await_triage' | 'live' | 'needs_human' | 'unknown'
  reason?: string
}

// issue #2259 (item 2 of #2165): "propose constitution" onboarding session.

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
  /** Offer a "propose constitution" session (issue #2259): true once the map
   * is merged AND the repo's governance document has an actual gap. Optional
   * on the type (not the wire — the API always sends it) so call sites that
   * mock a narrower `OnboardingView` shape need not carry a field they don't
   * exercise; the UI treats a missing value the same as `false`. */
  offerConstitution?: boolean
}

export function fetchOnboarding(slug: string): Promise<OnboardingView> {
  return api.get<OnboardingView>(
    `/pm/projects/${encodeURIComponent(slug)}/onboarding`)
}

export function proposeMap(slug: string): Promise<{ sessionId: string | null }> {
  return api.post<{ sessionId: string | null }>(
    `/pm/projects/${encodeURIComponent(slug)}/propose-map`)
}

export function proposeConstitution(slug: string): Promise<{ sessionId: string | null }> {
  return api.post<{ sessionId: string | null }>(
    `/pm/projects/${encodeURIComponent(slug)}/propose-constitution`)
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

export interface RetireCapabilityInput {
  slug: string
}

export interface RetireCapabilityResult {
  pointer: MapChangesPointer
}

export interface MergeCapabilitiesInput {
  fromSlug: string
  intoSlug: string
}

export interface MergeCapabilityResult {
  pointer: MapChangesPointer
  workItems: number
  initiatives: number
  linksRepointed: number
  linksNotRepointed: number
  childrenReparented: number
}

/** One capability edit on the product's map-changes branch (R8c-1/R8c-2: a dialog
 *  sends one op per request, in sequence, for only the fields that changed). */
export function editMapChanges(slug: string, input: MapChangeInput): Promise<MapChangeResult> {
  return api.post<MapChangeResult>(`/pm/projects/${encodeURIComponent(slug)}/map-changes`, input)
}

export function retireCapability(
  slug: string, input: RetireCapabilityInput,
): Promise<RetireCapabilityResult> {
  return api.post<RetireCapabilityResult>(
    `/pm/projects/${encodeURIComponent(slug)}/map-changes/retire`, input)
}

export function mergeCapabilities(
  slug: string, input: MergeCapabilitiesInput,
): Promise<MergeCapabilityResult> {
  return api.post<MergeCapabilityResult>(
    `/pm/projects/${encodeURIComponent(slug)}/map-changes/merge`, input)
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

/** The product overview's hygiene notice (spec Sustain: health readout — server
 *  contract `server/api/pm.py` `GET /pm/projects/{slug}/health`, merged in #1597):
 *  capabilities and initiatives the map nesting doesn't reach, capabilities still
 *  `proposed` after their map-changes PR merged, and inbox items no one triaged. */
export interface UnindexedDoc {
  path: string
  error: string
}

/** A path a capability's `code:`/`tests:`/`specs:`/`adrs:` frontmatter cites
 *  that no longer exists on the default branch (#2162). */
export interface DanglingEvidence {
  capability: string
  kind: string
  path: string
}

/** A frontmatter `capabilities:` entry (issue #2161) that names no indexed
 *  Feature in the project. */
export interface ArtifactUnknownCapability {
  initiative: string
  capability: string
}

export interface ProjectHealth {
  unparentedCapabilities: string[]
  unlinkedInitiatives: string[]
  staleProposed: string[]
  staleInbox: number
  unindexedDocs: UnindexedDoc[]
  governance: string | null
  danglingEvidence: DanglingEvidence[]
  untestedCapabilities: string[]
  codeWithoutSpecs: string[]
  stateBehindSpec: string[]
  /** Artifact frontmatter contract (issue #2161): initiatives whose committed
   *  artifact names an `epic:` that's closed while the initiative isn't `done`. */
  artifactsEpicClosedNotDone?: string[]
  /** Frontmatter `capabilities:` entries naming no indexed Feature. */
  artifactsUnknownCapability?: ArtifactUnknownCapability[]
  /** Initiatives with a committed artifact but no frontmatter fence at all. */
  artifactsMissingFrontmatter?: string[]
  generatedAt: string
}

export function fetchProjectHealth(slug: string): Promise<ProjectHealth> {
  return api.get<ProjectHealth>(`/pm/projects/${encodeURIComponent(slug)}/health`)
}

// --- dispatch outcome history (spec §6/§7; server contract `server/pm/outcomes.py`,
// `server/api/pm.py:get_work_item_outcomes`) ---

// Mirrors `server/db/models.py:DISPATCH_OUTCOMES` — `superseded` is a dispatch a
// newer one replaced before it could answer (never counted as a bounce).
export const DISPATCH_OUTCOMES = [
  'running', 'merged', 'no_pr', 'pr_gave_up', 'needs_human', 'reverted', 'abandoned',
  'superseded',
] as const
export type DispatchOutcomeKind = (typeof DISPATCH_OUTCOMES)[number]

/** One Backlog Runner dispatch of one issue — the history behind the roadmap's
 *  "bounced N× — needs spec" chip (D8). */
export interface DispatchOutcome {
  id: string
  targetRef: string
  projectId: string | null
  sessionId: string | null
  model: string | null
  attemptNo: number | null
  outcome: DispatchOutcomeKind | string
  cause: string | null
  prNumber: number | null
  prUrl: string | null
  /** Whether PR Doctor never had to recover this PR — `null` when its attempt
   *  count was unknown at the moment the runner's sweep saw the merge (never
   *  guessed, spec §6). */
  firstPassMerge: boolean | null
  bodyFingerprint: string | null
  startedAt: string | null
  endedAt: string | null
  durationS: number | null
  createdAt: string | null
}

/** Every dispatch a work item has had, newest first. */
export async function fetchOutcomes(workItemId: string): Promise<DispatchOutcome[]> {
  const res = await api.get<{ items: DispatchOutcome[] }>(
    `/pm/work-items/${encodeURIComponent(workItemId)}/outcomes`)
  return res.items
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

/** Full mirrored issue content and the opaque revision required for a decision. */
export interface WorkItemIssue {
  id: string
  ref: string
  repo: string
  number: number
  title: string
  body: string
  state: string
  url: string | null
  version: string
  /** Why this issue is held beyond its tier (hold ladder, spec 2026-09-18 §3),
   *  or null when it is not held. Rendered through `holdLabel`. */
  holdKind: string | null
  /** The runner's own words for why it gave up, or null. */
  holdReason: string | null
  /** Who or what set the hold — an identity or a subsystem name — or null. */
  holdBy: string | null
  /** When the hold was set, or null when there is none. */
  holdSetAt: string | null
  /** What Retry and Implement now each do from here: one `Consequence` per
   *  control, computed server-side by `server/pm/explain.py:hold_guidance`
   *  under the owning product's dispatch policy. For an `evidence` hold this is
   *  instead a single, button-less `Consequence` describing what the next
   *  Backlog Runner run does on its own (spec 2026-09-18 §5) — rendered through
   *  the same `ConsequenceList`, never a second renderer. Null for every other
   *  `holdKind` (`stronger_model`, `decision`) and for an unheld issue — the
   *  ladder climb and the decision inbox, other items in the series, have no
   *  copy here yet. */
  guidance?: { retry: Consequence; implement: Consequence } | { evidence: Consequence } | null
}
export type IssueDispositionInput = { version: string } & (
  | { action: 'edit'; title: string; body: string }
  | { action: 'resolve' | 'dismiss' }
)
export interface IssueDispositionResult { ok: true; item: WorkItemIssue }
export function fetchWorkItemIssue(itemId: string): Promise<WorkItemIssue> {
  return api.get(`/pm/work-items/${encodeURIComponent(itemId)}/issue`)
}
export function dispositionIssue(itemId: string, input: IssueDispositionInput): Promise<IssueDispositionResult> {
  return api.post(`/pm/work-items/${encodeURIComponent(itemId)}/issue`, input)
}

export interface HoldReleaseResult { ok: true; released: string | null; item: Record<string, unknown> }
export function releaseHold(itemId: string): Promise<HoldReleaseResult> {
  return api.post(`/pm/work-items/${encodeURIComponent(itemId)}/hold/release`)
}
