import { api } from './client'

export interface AutomationStatus {
  text: string
  level: 'idle' | 'active' | 'warn' | 'error'
  counts: Record<string, number> | null
}

export interface ConfigField {
  key: string
  label: string
  type: 'bool' | 'int' | 'string' | 'enum' | 'repo-multiselect'
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
  configSchema: ConfigField[]
  config: Record<string, unknown>
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
}

export function fetchAutomationActionable(id: string): Promise<ActionableItem[]> {
  return api.get<ActionableItem[]>(`/automations/${id}/actionable`)
}

export function postAutomationTrigger(
  id: string, body: { item_id: string; kind: string },
): Promise<TriggerResult> {
  return api.post<TriggerResult>(`/automations/${id}/trigger`, body)
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
