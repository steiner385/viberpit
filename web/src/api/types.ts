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
  | 'condensing'
  | 'waiting_input'
  | 'waiting_question'
  | 'ready_review'
  | 'done'
  | 'complete'
  | 'needs_attention'
  | 'limited'
  | 'ended'

/** A sub-agent (Task/Agent call) summary, nested under its session in the list. */
export interface SubAgentSummary {
  agentId: string            // the spawning Task tool-use id (= key into reducer state.subAgents)
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
  /** Last-activity time. May be null/absent for a session with no recorded
   *  activity — guard with isValidTimestamp() before formatting. */
  updatedAt?: string | null
  detached?: boolean
  nameIsAuto?: boolean
  /** Sub-agents spawned in this session, for hierarchical drill-down (empty when none). */
  subAgents?: SubAgentSummary[]
  /** True when the user has pinned the account (disables auto-rotation). */
  accountPinned?: boolean
  /** Current model alias/id for the session; null = account default. */
  model?: string | null
  /** Cached next-prompt suggestions for a live session (empty when none/detached). */
  latestSuggestions?: string[]
  /** Active /goal condition for this session; null/absent = no goal. */
  activeGoal?: string | null
  /** The PR opened from this session's branch, or null when there is none. */
  pr?: PullRequestInfo | null
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

/** Result of a best-effort batch operation over session ids. */
export interface BatchResult {
  succeeded: string[]
  failed: { id: string; error: string }[]
}

/** A live `claude` session running outside ccswitch, discovered for adoption. */
export interface AdoptionCandidate {
  pid: number | null
  account: string | null
  cwd: string
  claudeSessionId: string
  name: string
  mapped: boolean
  tracked: boolean
  /** Machine code; the UI maps it to plain-language copy. */
  skipReason: 'unmapped' | 'tracked' | null
  /** ISO timestamp of the transcript's last activity — drives the risk signal. */
  lastActivity: string
  pidAlive: boolean
}

/** Honest result of a force-takeover adoption. */
export interface AdoptOutcome {
  session: Session
  /** false when the transcript couldn't be resumed and a fresh session was started. */
  resumed: boolean
  /** The PID that was terminated, or null if the process had already exited. */
  terminatedPid: number | null
}

export interface PullRequestInfo {
  number: number
  url: string
  state: 'open' | 'draft' | 'merged' | 'closed'
  checks: 'passing' | 'failing' | 'pending' | 'none'
  title: string
  deployed: boolean
}

export interface ModelOption { id: string; label: string }

export interface ApiError {
  status: number
  error: { code: string; message: string }
}

export interface AllLimitedError extends ApiError {
  earliestReset: string | null
}
