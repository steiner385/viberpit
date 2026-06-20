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

export type SessionStatus = 'working' | 'idle' | 'limited' | 'ended'

export interface Session {
  id: string
  name: string
  accountName: string
  status: SessionStatus
  createdAt: string
  updatedAt: string
}

export interface ApiError {
  status: number
  error: { code: string; message: string }
}

export interface AllLimitedError extends ApiError {
  earliestReset: string | null
}
