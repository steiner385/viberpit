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
 *  relies on this shape; phase 2 defines it). */
export interface OverlayEdits {
  reach?: number
  impact?: number
  effort?: number
  confidence?: number
  featureId?: string | null
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
