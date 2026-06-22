export type AccountStatus = 'ok' | 'limited' | 'needs-relogin' | 'needs-reauth' | 'not-max' | 'unknown'

export interface ReauthState {
  state: 'idle' | 'running' | 'cancelling' | 'success' | 'error' | 'timeout'
  message?: string | null
  startedAt?: number | null
}

export interface UsageWindow {
  pct: number        // 0–100
  resetsAt: string | null  // ISO timestamp or null
}

export interface Account {
  name: string
  status: AccountStatus
  usage: {
    session: UsageWindow
    weekly: UsageWindow
  } | null
  nickname?: string | null
}

export type SessionStatus =
  | 'idle'
  | 'in_progress'
  | 'waiting_input'
  | 'ready_review'
  | 'done'
  | 'limited'
  | 'ended'

export interface SubAgentSummary {
  agentId: string
  parentAgentId: string | null
  agentType: string
  description: string
  status: 'running' | 'done' | 'failed'
}

export interface Session {
  id: string
  name: string
  accountName: string
  /** GitHub nameWithOwner ("owner/name") this session works on, or null for a scratch session. */
  repo?: string | null
  status: SessionStatus
  createdAt: string
  updatedAt: string
  detached?: boolean
  nameIsAuto?: boolean
  subAgents?: SubAgentSummary[]
}

export interface ArchivedSession {
  id: string
  name: string
  account: string
  cwd: string
  claudeSessionId: string | null
  permissionMode: string
  createdAt: string
  archivedAt: string
}

export interface ApiError {
  status: number
  error: { code: string; message: string }
}

export interface AllLimitedError extends ApiError {
  earliestReset: string | null
}
