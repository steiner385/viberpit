import { api } from './client'
import type { WorkClass } from './types'

/** One product's Backlog Runner numbers (spec §9/§10) — a status-card detail,
 *  not folded into `counts`, whose web type stays int-only. */
export interface AutomationStatusProduct {
  slug: string
  dispatch: string
  inFlight: number
  cap: number
  eligible: number
  bounced: number
  /** Items parked by an owner_action / decision hold (spec 2026-09-30 §4.1). */
  held?: number
  blocked?: number
  /** Every skip reason for this product, keyed as `server/pm/queue.py:SKIP_REASONS`. */
  skipped?: Record<string, number>
}

export interface AutomationStatus {
  text: string
  level: 'idle' | 'active' | 'warn' | 'error' | 'unknown'
  /** `policy` is the backlog runner's admission wait (`backlog_runner.py`,
   *  `waiting_reason="policy"`); it was on the wire and missing here. */
  waitingReason?: 'schedule' | 'human' | 'capacity' | 'budget' | 'policy'
  observedAt?: string
  lastRefreshFailedAt?: string
  counts: Record<string, number> | null
  /** Per-product Backlog Runner detail (spec §9), or null/absent for an
   *  automation with no per-product policy (every automation but the runner). */
  products?: AutomationStatusProduct[] | null
}

export interface ConfigField {
  key: string
  label: string
  type: 'bool' | 'int' | 'string' | 'enum' | 'repo-multiselect' | 'repo-number-map' | 'string-list'
  default: unknown
  help: string
  choices: string[] | null
  /** Inclusive lower bound for an `int` field, or null/absent when unbounded.
   *  What `choices` is for an enum — the server enforces it (422) and publishes
   *  it here, so the number input renders the same floor instead of hard-coding
   *  one. Optional so older fixtures still compile. */
  min?: number | null
  /** Which page edits the field (spec 2026-09-20 D7). `'capacity'`: Delivery → Capacity
   *  owns it and the Config tab renders it read-only. Optional so older fixtures compile. */
  surface?: 'config' | 'capacity'
  /** On its way out (spec 2026-09-20 D6): still stored and validated, read by
   *  nothing, hidden by every editor. Optional so older fixtures still compile. */
  deprecated?: boolean
  /** null is a legal stored value (blank = unlimited). Optional so older fixtures still compile. */
  nullable?: boolean
}

export interface Automation {
  id: string
  name: string
  description: string
  enabled: boolean
  mode: string | null
  modes: string[]
  status: AutomationStatus
  /** One server snapshot shared by navigation, list, and detail. Missing means unknown.
   *  Counts DECISIONS only (`ActionableItem.kind === 'decision'`): what a human
   *  must decide. This is what the tier and the nav badge read. */
  actionableCount?: number | null
  /** Housekeeping the automation already settled and merely lets a human revisit
   *  (un-mute, override a throttle). Shown, never badged. Missing means unknown. */
  maintenanceCount?: number | null
  /** The decisions of a PAUSED automation (disabled or mode off). The server
   *  publishes them here instead of `actionableCount`, so they never badge;
   *  they stay listed and actionable on the automation's own page (#2936).
   *  0 when not paused; missing means unknown. */
  parkedCount?: number | null
  /** Disabled or mode off, and has a loop to pause. The server's rule,
   *  published so the browser never re-derives it. */
  paused?: boolean
  oversight?: {
    responsibility: { id: string; label: string }
    scope: string
    modeDescriptions: Record<string, string>
    relationships: {
      kind: 'requires' | 'uses' | 'hands_off_to' | 'shares_work_with'
      targetId: string
      description: string
    }[]
  }
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
  /** Whether this origin's sessions are new work or housekeeping the sessions
   *  list keeps in its Background scope. Decided server-side in
   *  `automations.base.work_class_of`; optional so older fixtures still compile. */
  workClass?: WorkClass
}

export interface AutomationEvent {
  ts: string
  level: string
  kind: string | null
  summary: string
  target: string | null
  sessionId: string | null
  // Free-form structured context the server attaches to some events (e.g. a
  // `refill_stalled` reason and its slot holders). Null on most rows.
  detail: Record<string, unknown> | null
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
  /** Plain text shown instead of a button when a live predecessor makes the
   *  action a guaranteed refusal (`actions` is empty then). */
  status?: string | null
  /** `decision` (the automation waits on a human) or `maintenance` (settled
   *  housekeeping a human may revisit). Absent on a pre-upgrade payload = decision. */
  kind?: 'decision' | 'maintenance'
}

/** The one place the kind is read: a row is a decision unless it says otherwise. */
export function isDecision(item: Pick<ActionableItem, 'kind'>): boolean {
  return item.kind !== 'maintenance'
}

/** Decisions first, housekeeping apart — the Needs you tab, its badge and the
 *  Overview preview all count the first list only. */
export function splitActionable<T extends Pick<ActionableItem, 'kind'>>(items: T[]): { decisions: T[]; maintenance: T[] } {
  const decisions: T[] = []
  const maintenance: T[] = []
  for (const item of items) (isDecision(item) ? decisions : maintenance).push(item)
  return { decisions, maintenance }
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

// --- The clustered inbox (#2936) ---------------------------------------------

/** Automations whose Needs you tab is the clustered inbox rather than the flat
 *  actionable list (the server answers `/clusters` for these only). */
export const INBOX_AUTOMATIONS: ReadonlySet<string> = new Set(['session-feedback'])

export interface InboxReport {
  id: string
  title: string
  category: string | null
  impact: string | null
  fingerprint: string | null
  reportedAt: string | null
  status: string | null
  /** The reporting session's name (or id). */
  session: string | null
}

export interface ReportCluster {
  /** Stable for a given member set. */
  id: string
  /** The model's theme when `themed`, else the most common member title. */
  title: string
  count: number
  category: string | null
  /** The most severe member impact. */
  impact: string | null
  firstReported: string | null
  lastReported: string | null
  themed: boolean
  fingerprints: string[]
  /** Newest first. */
  members: InboxReport[]
  /** Up to two merged PRs whose titles match, merged after the first report —
   *  a hint, never applied on its own. Best first. */
  possiblyFixed: PossibleFix[]
}

export interface PossibleFix {
  number: number
  title: string
  url: string | null
  mergedAt: string | null
  /** Title-word overlap, 0–1. */
  score: number
}

export interface InboxClusters {
  clusters: ReportCluster[]
  /** The model merge pass: answered for this inbox, catching up with it, failed
   *  (similarity groups only), or turned off in the config. */
  llm: 'applied' | 'pending' | 'failed' | 'off'
  total: number
  /** Reports that aged out of the inbox (never badged): the exact count and the
   *  newest few, each restorable. */
  digest: { count: number; items: InboxReport[] }
}

export interface ClusterDraft extends TriggerResult {
  title: string
  body: string
}

/** Exactly one target: an existing issue, or a live session. */
export type ClusterAttachTarget = { issue: number } | { sessionId: string }

export function fetchInboxClusters(id: string): Promise<InboxClusters> {
  return api.get<InboxClusters>(`/automations/${id}/clusters`)
}

export function postClusterDraft(id: string, ids: string[]): Promise<ClusterDraft> {
  return api.post<ClusterDraft>(`/automations/${id}/clusters/draft`, { ids })
}

export function postClusterFile(
  id: string, body: { ids: string[]; title: string; body: string },
): Promise<TriggerResult> {
  return api.post<TriggerResult>(`/automations/${id}/clusters/file`, body)
}

export function postClusterDismiss(
  id: string, body: { ids: string[]; fixedBy?: number },
): Promise<TriggerResult> {
  return api.post<TriggerResult>(`/automations/${id}/clusters/dismiss`, body)
}

export function postClusterAttach(
  id: string, body: { ids: string[] } & ClusterAttachTarget,
): Promise<TriggerResult> {
  return api.post<TriggerResult>(`/automations/${id}/clusters/attach`, body)
}

export function postClusterRestore(id: string, body: { ids: string[] }): Promise<TriggerResult> {
  return api.post<TriggerResult>(`/automations/${id}/clusters/restore`, body)
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
  /** The grouping model's reply could not be parsed, so this generation is the
   *  parser's all-singletons fallback rather than a real plan (#2217). */
  degraded: boolean
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
  status: 'spawned' | 'no_account' | 'refused' | string
  session_id: string | null
  session_name: string | null
  // With 'spawned': the session exists but waits for a free slot (a manual
  // dispatch at the runner/product cap queues instead of failing).
  queued?: boolean
}

/** The success toast for a dispatch whose session exists: "Queued …" while it
 *  waits for a slot, "Started …" otherwise. One copy for every dispatch surface. */
export function dispatchStartedText(r: Pick<DispatchGroupResponse, 'queued' | 'session_name'>): string {
  const verb = r.queued ? 'Queued' : 'Started'
  return r.session_name ? `${verb} ${r.session_name}` : `${verb} a session`
}

export function fetchBacklogCandidates(): Promise<BacklogCandidatesView> {
  return api.get<BacklogCandidatesView>('/automations/backlog-runner/candidates')
}

export function postDispatchGroup(refs: string[]): Promise<DispatchGroupResponse> {
  return api.post<DispatchGroupResponse>('/automations/backlog-runner/dispatch', { refs })
}
