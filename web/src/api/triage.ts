/**
 * PM triage API client — types + fetchers over `/api/pm/triage` (phase 2).
 * This is the CANONICAL triage module — phase 3 imports from here and upgrades
 * the hooks in place; it never forks a second TriageProposal/fetchTriage.
 */
import { api } from './client'

// Re-export the map/classify spawn fetchers so triage callers (and phase 3) have
// one import source for the whole triage surface.
export { proposeMap, runClassify as classify } from './pm'

export interface TriageProposal {
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

/** Per-id outcome of an accept-selected sweep — partial success is normal, so
 *  the endpoint reports each id rather than failing the whole batch. */
export interface AcceptBulkResult {
  accepted: number
  results: { id: string; ok: boolean; error?: string }[]
}

export function fetchTriage(projectSlug?: string): Promise<TriageProposal[]> {
  const qs = projectSlug
    ? `?projectSlug=${encodeURIComponent(projectSlug)}`
    : ''
  return api.get<TriageProposal[]>(`/pm/triage${qs}`)
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

/** Accept many proposals in one round trip (the inbox's `A` / accept-selected). */
export function acceptBulk(ids: string[]): Promise<AcceptBulkResult> {
  return api.post<AcceptBulkResult>('/pm/triage/accept-bulk', { ids })
}

/** Edit a work item's overlay directly (inline score edits, triage-state moves). */
export function updateOverlay(
  itemId: string, edit: OverlayEdits,
): Promise<WorkItemOverlay> {
  return api.patch<WorkItemOverlay>(
    `/pm/work-items/${encodeURIComponent(itemId)}/overlay`, edit)
}
