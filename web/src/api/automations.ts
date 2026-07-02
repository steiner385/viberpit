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

// --- PR Doctor propose surface (one-tap actions) -----------------------------

export type PrDoctorKind = 'out_of_date' | 'failing_ci' | 'merge_conflict'

export interface PrDoctorActionablePr {
  repo: string
  number: number
  kind: PrDoctorKind
  url: string
  title: string
}

export function fetchPrDoctorActionable(): Promise<PrDoctorActionablePr[]> {
  return api.get<PrDoctorActionablePr[]>('/automations/pr-doctor/actionable')
}

export function postPrDoctorFix(
  body: { repo: string; number: number; kind: PrDoctorKind },
): Promise<{ status: string }> {
  return api.post<{ status: string }>('/automations/pr-doctor/fix', body)
}
