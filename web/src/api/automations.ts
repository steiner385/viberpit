import { api } from './client'

export interface AutomationStatus {
  text: string
  level: 'idle' | 'active' | 'warn' | 'error'
  counts: Record<string, number> | null
}

export interface ConfigField {
  key: string
  label: string
  type: 'bool' | 'int' | 'string' | 'enum' | 'repo-multiselect' | 'repo-number-map'
  default: unknown
  help: string
  choices: string[] | null
}

export interface Automation {
  id: string
  name: string
  description: string
  enabled: boolean
  mode: string | null
  modes: string[]
  status: AutomationStatus
  /** Audited agent-engine ceiling for this origin (Phase 7). `claude_only` means
   *  the origin has no Codex acceptance evidence, so `configSchema` carries no
   *  engine selector — the absence IS the enforcement, and the card must not
   *  synthesize one. Optional so older fixtures still compile. */
  enginePolicy?: 'claude_only' | 'codex_allowed' | 'codex_only'
  /** True for an origin with no control loop: it spawns only when an operator (or
   *  another automation) calls its seam, so there is nothing to enable or
   *  schedule and the server 422s a PATCH that tries. Decided server-side in
   *  `automations.base.operator_triggered`; the `modes` fallback below is only so
   *  older fixtures compile. */
  operatorTriggered?: boolean
  configSchema: ConfigField[]
  config: Record<string, unknown>
  /** Optional user-configured cadence ({activeHours?, cron?}), or null (#603).
   *  Always present in the list API; typed optional so older fixtures still compile. */
  schedule?: Record<string, unknown> | null
  lastActiveAt: string | null
  /** Count of sessions this automation spawned that are still live (not ended/archived). */
  activeSessions: number
}

export interface AutomationEvent {
  ts: string
  level: string
  kind: string | null
  summary: string
  target: string | null
  sessionId: string | null
}

export interface AutomationPatch {
  enabled?: boolean
  mode?: string
  config?: Record<string, unknown>
  /** Set to {} to clear the schedule, or a {activeHours?, cron?} object (#603). */
  schedule?: Record<string, unknown>
}

export function fetchAutomations(): Promise<Automation[]> {
  return api.get<Automation[]>('/automations')
}

export function fetchAutomationEvents(
  id: string, params: { target?: string; kind?: string; limit?: number } = {},
): Promise<AutomationEvent[]> {
  const q = new URLSearchParams()
  if (params.target) q.set('target', params.target)
  if (params.kind) q.set('kind', params.kind)
  if (params.limit) q.set('limit', String(params.limit))
  const qs = q.toString()
  return api.get<AutomationEvent[]>(`/automations/${id}/events${qs ? `?${qs}` : ''}`)
}

export function fetchSpawnedSessions(
  id: string,
): Promise<{ id: string; name: string; status: string; originRef: string | null }[]> {
  return api.get(`/automations/${id}/sessions`)
}

export function patchAutomation(id: string, body: AutomationPatch): Promise<Automation> {
  return api.patch<Automation>(`/automations/${id}`, body)
}

// --- Generic automation actionable/trigger surface ---------------------------

export interface ActionSpec {
  kind: string
  label: string
  style: 'default' | 'risky'
}

export interface ActionableItem {
  id: string
  title: string
  subtitle?: string | null
  url?: string | null
  actions: ActionSpec[]
}

export interface TriggerResult {
  status: string
  level: 'ok' | 'warn' | 'error'
  message: string
  /** Async triggers (issue #668) answer status "queued" with a job id the UI
   *  polls for live phase progress. Absent on synchronous results. */
  job_id?: string
  phase?: string
}

/** Live state of an async pr-doctor trigger job (issue #668). Phases advance
 *  cloning → assessing → spawning → ready; on failure `errorPhase` names the
 *  stage that failed. Statuses: queued | running | ready | error | deferred. */
export interface PrDoctorJob {
  job_id: string
  target: string
  kind: string
  status: string
  phase: string
  session_id: string | null
  error_phase: string | null
  message: string
  level: 'ok' | 'warn' | 'error'
}

export function fetchAutomationActionable(id: string): Promise<ActionableItem[]> {
  return api.get<ActionableItem[]>(`/automations/${id}/actionable`)
}

export function postAutomationTrigger(
  id: string, body: { item_id: string; kind: string },
): Promise<TriggerResult> {
  return api.post<TriggerResult>(`/automations/${id}/trigger`, body)
}

export function fetchPrDoctorJob(jobId: string): Promise<PrDoctorJob> {
  return api.get<PrDoctorJob>(`/automations/pr-doctor/jobs/${jobId}`)
}

// --- PR-doctor consolidation -------------------------------------------------

export interface ConsolidationCandidate {
  number: number
  title: string
  url: string
  checks: string
  mergeable: string
  updated_at: string
  scope: string
}

export interface ConsolidationCluster {
  numbers: number[]
  rationale: string
  risk: 'low' | 'risky'
}

/** A live consolidation session and the PR numbers it covers. */
export interface ActiveConsolidation {
  session_id: string
  numbers: number[]
}

export interface ConsolidationView {
  repos: Record<string, {
    candidates: ConsolidationCandidate[]
    clusters: ConsolidationCluster[]
    active: ActiveConsolidation[]
  }>
}

export function fetchConsolidation(): Promise<ConsolidationView> {
  return api.get<ConsolidationView>('/automations/pr-doctor/consolidation')
}

export function postConsolidate(
  body: { repo: string; numbers: number[] },
): Promise<TriggerResult> {
  return api.post<TriggerResult>('/automations/pr-doctor/consolidate', body)
}

// --- Backlog Runner grouping plan --------------------------------------------

/** One precomputed group of related issues planned into a single PR. */
export interface BacklogGroup {
  id: number
  repo: string | null
  refs: string[]
  titles: Record<string, string>
  rationale: string | null
  rank: number | null
  generation: string | null
  createdAt: string | null
  consumedAt: string | null
}

export interface BacklogGroupsView {
  generation: string | null
  createdAt: string | null
  groups: BacklogGroup[]
}

export function fetchBacklogGroups(): Promise<BacklogGroupsView> {
  return api.get<BacklogGroupsView>('/automations/backlog-runner/groups')
}

export interface BacklogCandidate {
  ref: string
  title: string
  repo: string
  number: number
  body: string
  url: string
}

export interface BacklogCandidatesView {
  candidates: BacklogCandidate[]
}

export interface DispatchGroupResponse {
  // 'refused': the launch was declined before a session existed (an engine the
  // origin is not audited for, or an engine/permission-mode pair that could not
  // open the PR). The claim is returned to the pool; the reason is in the feed.
  status: 'spawned' | 'claim_failed' | 'no_account' | 'refused' | string
  session_id: string | null
  session_name: string | null
}

export function fetchBacklogCandidates(): Promise<BacklogCandidatesView> {
  return api.get<BacklogCandidatesView>('/automations/backlog-runner/candidates')
}

export function postDispatchGroup(refs: string[]): Promise<DispatchGroupResponse> {
  return api.post<DispatchGroupResponse>('/automations/backlog-runner/dispatch', { refs })
}
