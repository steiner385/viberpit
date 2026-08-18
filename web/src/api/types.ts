export type AccountStatus = 'ok' | 'limited' | 'needs-relogin' | 'needs-reauth' | 'not-max' | 'unknown'

export interface ReauthState {
  state: 'idle' | 'running' | 'cancelling' | 'success' | 'error' | 'timeout' | 'awaiting_code'
  message?: string | null
  account?: string | null   // the account/slot this operation targets (scopes per-account status)
  startedAt?: number | null
  vncSession?: string | null
  authUrl?: string | null
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
  provider?: 'anthropic' | 'copilot'
  usage: {
    session: UsageWindow
    weekly: UsageWindow
  } | null
  nickname?: string | null
  weeklyReservePct?: number | null
  liveSessions: LiveSessionCounts
  rank: number | null
  rankReason: string | null
  accountDefaults?: AccountDefaults | null
  // Absent (older payloads) is treated as enabled. A disabled account is kept
  // intact but blocked everywhere in selection until re-enabled.
  enabled?: boolean
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
  | 'queued'

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
  /** The reckoned goal autopilot drives toward (effective_goal resolver). */
  effectiveGoal?: string | null
  /** Where the reckoned goal came from: "explicit" | "inferred" | "initial_prompt". */
  goalSource?: string | null
  /** Per-session autopilot opt-out; true = pinned to manual, false/absent = governed. */
  autopilotPaused?: boolean
  /** ISO instant until which autopilot is deferring because the user is steering this
   *  session; null/absent = not deferring. The card counts down locally from this. */
  autopilotCooldownUntil?: string | null
  /** Sticky auto-archive opt-out reason (e.g. "manual-unarchive"); set when a human
   *  unarchived the session, so autopilot never auto-archives it again. Null/absent = eligible. */
  autoArchiveOptOut?: string | null
  /** Automation that has acted on this human-created session (e.g. "autopilot").
   *  Distinct from origin (spawned-by). Drives the violet provenance badge. */
  assistedBy?: string | null
  /** Row-write timestamp used to order poll vs. WS updates; bumped on every status persist.
   *  Distinct from updatedAt (freshness). */
  revAt?: string | null
  /** The PRs authored from this session's branches. */
  prs: PullRequestInfo[]
  /** True when the session was created without a verified usage read (optimistic pick). */
  usageUnverified?: boolean
  /** Issue #434: the session's in-flight turn was dropped by a daemon wedge/restart (it
   *  came back idle via rehydrate). The dashboard labels it "re-send to resume". Transient. */
  interruptedByRestart?: boolean
  /** The automation_id that spawned this session (e.g. 'pr-doctor'); null/absent = human-created. */
  origin?: string | null
  /** The automation's reference key (e.g. 'owner/repo#123'); null/absent when origin is absent. */
  originRef?: string | null
  /** Client-only: true while an optimistic create placeholder awaits the server. Never sent by the server. */
  pending?: boolean
  /** Position in the max-active-sessions queue (1 = next to run); null when not queued. */
  queuePosition?: number | null
}

export interface ArchivedSession {
  id: string
  name: string
  account: string
  cwd: string
  /** GitHub nameWithOwner (e.g. "cairnea/KinDash"); null for scratch/legacy rows. */
  repo: string | null
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
  /** Estimated seconds until CI completes (snake_case to match backend to_dict). */
  eta_seconds?: number | null
  /** Estimated completion percent (snake_case to match backend to_dict). */
  eta_percent?: number | null
  /** True when the CI run has exceeded its expected duration. */
  eta_overdue?: boolean
  /** Lower-bound ETA seconds (optimistic confidence band). */
  eta_low_seconds?: number | null
  /** Upper-bound ETA seconds (pessimistic confidence band). */
  eta_high_seconds?: number | null
  /** Per-check drill-down groups (head commit + merge group); snake_case to match backend. */
  check_groups?: CheckGroup[]
}

export interface CheckRow {
  name: string
  workflow: string | null
  status: string
  conclusion: string
  duration_seconds: number | null
  required: boolean
  overdue: boolean
  rem_low_seconds?: number | null
  rem_nominal_seconds?: number | null
  rem_high_seconds?: number | null
}

export interface CheckGroup {
  kind: 'head' | 'merge'
  checks: CheckRow[]
  total_seconds: number
  cost_usd: number | null
  eta_seconds?: number | null
}

export type NodeState = 'done' | 'active' | 'failed' | 'pending'

export interface StageNode {
  id: string
  label: string
  kind: 'ci' | 'gate' | 'queue' | 'deploy' | 'merged'
  order: number
  source: 'derived' | 'override'
}

export interface StageModel {
  repo: string
  nodes: StageNode[]
}

export interface PipelinePr extends PullRequestInfo {
  /** The ccswitch session that authored this PR's branch, or null. */
  session: { id: string; name: string } | null
  /** Per-node placement states for this PR's position on its repo's stage track. */
  node_states?: { id: string; state: NodeState }[]
}

export interface CheckDurationStat {
  check_name: string
  event: string
  p50: number
  p90: number
  ratio: number | null
  n: number
}

export interface CheckFlakeStat {
  check_name: string
  event: string
  flake_pct: number
  flaky_commits: number
  total_commits: number
  n: number
}

export interface CheckRegressionStat {
  check_name: string
  event: string
  prior_p50: number
  recent_p50: number
  ratio: number
  since: string
}

export interface RunnerWaitStat {
  event: string
  p50: number
  p90: number
  prev_p50: number | null
  n: number
}

export interface CheckInsights {
  repo: string
  window: string
  slowest: CheckDurationStat[]
  flakiest: CheckFlakeStat[]
  regressions: CheckRegressionStat[]
  runner_wait_health: RunnerWaitStat[]
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
  // `failed`/`gaveUp` are the daemon's LIVE unresolved set (reconciled edge-side),
  // not the controller's frozen deploy-time list — so the banner clears as autopilot
  // recovers sessions. `gaveUp` = capped-out recovery, kept visible for a human.
  restore?: { restored?: string[]; failed: string[]; gaveUp?: string[] }
}

export interface DeployStatusResponse {
  pending: PendingDeploy
  deploy: DeployProgress
}

/** Issue #434: daemon lifecycle health for the dashboard indicator. Mirrors the
 *  DaemonHealthMonitor snapshot from GET /api/daemon/health. `available` is false in
 *  in-process (dev) mode, which has no separate daemon to watch. */
export interface DaemonHealth {
  available: boolean
  /** Current daemon process epoch (`<start_ms_hex>-<pid>-<uuid8>`); null when unavailable. */
  epoch: string | null
  /** ISO time the current daemon generation started; null when unknown. */
  startedAt: string | null
  /** Seconds the current daemon generation has been up; null when unknown. */
  uptimeSec: number | null
  /** Restarts observed by the edge in the last 24h (0 = healthy/steady). */
  restartCount24h: number
  /** ISO time of the most recent observed restart (or the watchdog file), null if none. */
  lastRestartAt: string | null
}

export interface ApiError {
  status: number
  error: { code: string; message: string }
}

export interface AllLimitedError extends ApiError {
  earliestReset: string | null
}

export interface CopilotAddState {
  state: 'pending' | 'awaiting_authorization' | 'authorized' | 'failed'
  userCode: string | null
  verificationUri: string | null
}

// --- Library (plugins / skills / commands / MCP) ---

export interface LibraryComponentCounts {
  commands: number
  skills: number
  agents: number
  mcpServers: number
  hooks: number
}

export interface LibraryPlugin {
  id: string
  name: string
  marketplace: string
  enabled: boolean
  version?: string | null
  installPath?: string | null
  installedAt?: string | null
  lastUpdated?: string | null
  description?: string | null
  category?: string | null
  components: LibraryComponentCounts
  tokenCost?: { alwaysOn: number; onInvoke: number } | null
}

export interface LibraryPluginDetail extends LibraryPlugin {
  commands: { path: string; description?: string | null }[]
  skills: { name: string; description?: string | null }[]
  agents: string[]
  mcpServers: { name: string; transport: string }[]
  hasHooks: boolean
}

export interface LibraryMarketplace {
  name: string
  source?: string | null
  lastUpdated?: string | null
}

export interface LibraryPluginsResponse {
  plugins: LibraryPlugin[]
  marketplaces: LibraryMarketplace[]
}

export interface LibrarySkill {
  name: string
  description?: string | null
  source: string          // 'user' | plugin id
  readonly: boolean
  parseError?: boolean
}

export interface LibraryCommand {
  path: string
  name: string
  description?: string | null
  source: string
  readonly: boolean
  parseError?: boolean
}

export interface McpServerConfig {
  type?: string
  command?: string
  args?: string[]
  env?: Record<string, string>
  url?: string
  httpUrl?: string
  headers?: Record<string, string>
  [key: string]: unknown
}

export interface LibraryMcpServer {
  name: string
  source: string
  readonly: boolean
  transport: 'stdio' | 'http' | 'sse'
  config: McpServerConfig
  needsAuth: string[]
}

export interface LibraryContent {
  name?: string
  path?: string
  content: string
  readonly: boolean
}

// Per-account spawn defaults (#599)
export interface AccountDefaults {
  defaultModel: string | null
  defaultPermissionMode: string | null
  envOverrides: Record<string, string> | null
}

// Session Preset (#597)
export interface SessionPreset {
  id: string
  name: string
  repo: string | null
  account: string | null
  model: string | null
  permissionMode: string | null
  initialPrompt: string | null
  branchStrategy: string | null
  permissionProfileId: string | null
  createdAt: string | null
  updatedAt: string | null
}

// Prompt Template (#598)
export interface TemplateVariable {
  name: string
  description?: string
  default?: string
}

export interface PromptTemplate {
  id: string
  name: string
  content: string
  variables: TemplateVariable[]
  tags: string[]
  createdAt: string | null
  updatedAt: string | null
}

// Dispatch assess (#dispatch-auto-select)
export interface AssessResponse {
  model: string | null
  refined_prompt: string | null
}

// Permission Profile (#600)
export interface PermissionProfile {
  id: string
  name: string
  description: string | null
  mode: string | null
  patterns: string[]
  createdAt: string | null
  updatedAt: string | null
}
