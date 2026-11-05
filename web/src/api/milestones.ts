/**
 * Release-milestones API client (spec 2026-11-05 §9–§10). Plain fetchers here,
 * React Query hooks in `api/hooks.ts`. The types mirror the server's camelCase
 * view model verbatim (`server/pm/milestone_view.py:build_milestones_view`,
 * served by `server/api/pm_milestones.py`).
 */
import { api, ApiRequestError } from './client'

/** Read-time status (`server/pm/milestone_status.py`); `info` is a number with no target. */
export type ConditionStatus = 'met' | 'unmet' | 'unknown' | 'info'
export type ConditionOp = 'eq' | 'ne' | 'lt' | 'le' | 'gt' | 'ge'
export type MilestoneState = 'current' | 'done' | 'upcoming'
/** `server/pm/milestone_view.py:LEAF_STATES`. */
export type LeafState = 'done' | 'in_flight' | 'not_started'

/** A stored reading: a count, closed-of-total, or a manual tick. */
export type ConditionValue =
  | { n: number }
  | { done: number; total: number }
  | { met: boolean }
  | Record<string, unknown>

export interface MilestoneCondition {
  key: string
  label: string
  kind: string
  status: ConditionStatus
  value: ConditionValue | null
  target: number | null
  op: ConditionOp | null
  measuredAt: string | null
  ageSeconds: number | null
  error: string | null
  sourceUrl: string | null
  manualBy: string | null
  manualEvidence: string | null
}

export interface MilestonePlatform { name: string | null; off: boolean; note: string | null }
export interface MilestoneEpic { ref: string; url: string }
export interface LeafPr { number: number | null; url: string | null }

export interface MilestoneLeaf {
  ref: string
  title: string | null
  url: string | null
  state: LeafState
  prs: LeafPr[]
}

export interface MilestoneLeaves {
  done: number
  inFlight: number
  notStarted: number
  total: number
  measuredAt: string | null
  items: MilestoneLeaf[]
}

export interface GateSummary { met: number; total: number; unknown: number; ready: boolean }

export interface Milestone {
  key: string
  label: string | null
  name: string | null
  audience: string | null
  platforms: MilestonePlatform[]
  state: MilestoneState | null
  enteredAt: string | null
  exitedAt: string | null
  statusNote: string | null
  exitOverride: string | null
  epics: MilestoneEpic[]
  gate: MilestoneCondition[]
  gateSummary: GateSummary
  leaves: MilestoneLeaves
}

export interface MilestoneLadder {
  title: string | null
  specPath: string | null
  specUrl: string | null
  sourcePath: string | null
  sourceSha: string | null
  syncedAt: string | null
  parseError: string | null
  parseErrorSha: string | null
  currentKey: string | null
  currentRemoved: boolean
  updatedAt: string | null
  autoRefresh: boolean
}

export type MilestonesView =
  | { ladder: null }
  | { ladder: MilestoneLadder; milestones: Milestone[]; numbers: MilestoneCondition[] }

/** `POST …/refresh`: 202 stamped, 429 inside the window, 409 automatic refresh off. */
export type RefreshMilestonesResult =
  | { refreshed: true }
  | { refreshed: false; retryAfter: number }
  | { refreshed: false; reason: 'automatic_refresh_off' }

const base = (slug: string) => `/pm/projects/${encodeURIComponent(slug)}/milestones`

export function fetchMilestones(slug: string): Promise<MilestonesView> {
  return api.get<MilestonesView>(base(slug))
}

/** The 429 and 409 answers are outcomes the button renders, not failures. */
export async function refreshMilestones(slug: string): Promise<RefreshMilestonesResult> {
  try {
    return await api.post<RefreshMilestonesResult>(`${base(slug)}/refresh`)
  } catch (err) {
    if (err instanceof ApiRequestError && (err.status === 429 || err.status === 409)) {
      return err.body as RefreshMilestonesResult
    }
    throw err
  }
}
