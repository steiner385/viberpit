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

export interface LiveSessionCounts {
  active: number
  idle: number
}

export interface Account {
  name: string
  status: AccountStatus
  usage: {
    session: UsageWindow
    weekly: UsageWindow
  } | null
  nickname?: string | null
  liveSessions: LiveSessionCounts
  rank: number | null
  rankReason: string | null
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
  | 'waiting_external'
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
  /** One-line LLM status summary of what the session is doing now (empty when none). */
  summary?: string
  /** Active /goal condition for this session; null/absent = no goal. */
  activeGoal?: string | null
  /** The PRs authored from this session's branches. */
  prs: PullRequestInfo[]
  /** True when the session was created without a verified usage read (optimistic pick). */
  usageUnverified?: boolean
  /** Client-only: true while an optimistic create placeholder awaits the server. Never sent by the server. */
  pending?: boolean
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
  /** Open-stage classifier output: 'ci' | 'ready' | 'parked' | 'merged' | '' */
  stage?: string
  /** nameWithOwner of the PR's repo (populated for population/pipeline PRs). */
  repo?: string
}

export interface PipelinePr extends PullRequestInfo {
  /** The ccswitch session that authored this PR's branch, or null. */
  session: { id: string; name: string } | null
}

export interface ModelOption { id: string; label: string }

export interface PendingDeploy {
  behind: boolean
  liveSha: string
  targetSha: string
  commits: { sha: string; subject: string; author: string; iso: string }[]
  ci: string
}

export interface DeployProgress {
  running: boolean
  phases: Record<string, string>
  error: string | null
  warnings: string[]
  targetSha: string
  restore?: { restored: string[]; failed: string[] }
}

export interface DeployStatusResponse {
  pending: PendingDeploy
  deploy: DeployProgress
}

export interface ApiError {
  status: number
  error: { code: string; message: string }
}

export interface AllLimitedError extends ApiError {
  earliestReset: string | null
}
