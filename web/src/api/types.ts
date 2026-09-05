export type AccountStatus = 'ok' | 'limited' | 'needs-relogin' | 'needs-reauth' | 'not-max' | 'unknown'

export interface ReauthState {
  state: 'idle' | 'running' | 'cancelling' | 'success' | 'error' | 'timeout' | 'awaiting_code'
  message?: string | null
  account?: string | null   // the account/slot this operation targets (scopes per-account status)
  startedAt?: number | null
  vncSession?: string | null
  authUrl?: string | null
}

/** One metered allowance on one account.
 *
 *  A provider reports the windows it ACTUALLY meters — Anthropic sends a session
 *  (5-hour) and a weekly (7-day) window; Copilot sends one premium-request
 *  window. It used to send that single number twice, under two labels, because
 *  the wire shape had two fixed fields. Render the list, never a fixed pair. */
export interface UsageWindow {
  /** Stable machine key: 'session' | 'weekly' | 'premium' | … */
  key: string
  /** Human label the provider chose for this window, e.g. 'premium-req'. */
  label: string
  pct: number        // 0–100
  resetsAt: string | null  // ISO timestamp, or null when no window is active
  durationSeconds?: number | null
  /** 'account' (meters everything) or 'model' (meters `appliesToModels` only). */
  scope?: string
  appliesToModels?: string[]
}

/** How much a snapshot's windows can be trusted. Anything other than 'available'
 *  means the read failed — the windows array is then EMPTY rather than holding a
 *  fabricated 0%. */
export type UsageAvailability = 'available' | 'unknown' | 'auth_error'

export interface AccountUsage {
  provider: string
  fetchedAt: string | null
  availability: UsageAvailability
  windows: UsageWindow[]
  // Whether the PROVIDER has extra usage enabled — read-only, set in the
  // Anthropic console. Distinct from `allowExtraUsage` below, which is this
  // app's opt-in. Optional: older payloads predate the field.
  extraUsageEnabled?: boolean
}

export interface LiveSessionCounts {
  active: number
  idle: number
}

/** How the HUMAN proves who they are when enrolling/repairing an account. */
export type UserAuthMode = 'claude_oauth' | 'github_device' | 'chatgpt_device'

/** How a RUNNER is handed the credential for a launch. */
export type RunnerAuthMode = 'config_dir' | 'copilot_proxy' | 'chatgpt_external_tokens'

/** What the edge may say about the Codex credential authority itself.
 *  Mirrors `server/codex/auth_types.py:AuthorityHealth.to_wire`; never carries a
 *  token or a native account id. `null` on an account row means the edge has no
 *  authority configured (or the probe failed) — NOT that it is down. */
export interface CodexAuthorityHealth {
  reachable: boolean
  protocolVersion?: number
  /** Revision of the authorization projection in force, or null when none. */
  syncRevision?: number | null
  syncExpiresInS?: number | null
  accounts?: string[]
  detail?: string | null
}

/** The OpenAI-only block on an account row (`server/api/accounts.py:_codex_details`).
 *  Absent for every other provider. */
export interface CodexAccountInfo {
  authority: CodexAuthorityHealth | null
  /** Premium/plan quota, in the SAME window shape every provider uses — read it
   *  through `@/lib/usageWindows`, never by reaching for a named window. */
  quota: AccountUsage | null
  /** Live Codex runner processes currently holding this account's credential. */
  activeRunners: number
  /** Why the account is degraded (e.g. authority unreachable); null when healthy. */
  degradedReason: string | null
}

export interface Account {
  name: string
  status: AccountStatus
  /** 'unknown' when the account is not in the accounts.json manifest — the API
   *  says so rather than defaulting to 'anthropic' and offering the wrong
   *  recovery action. */
  provider?: 'anthropic' | 'copilot' | 'openai' | 'unknown'
  /** Which agent this account can launch. Absent on a pre-Codex payload and for
   *  an account missing from the manifest; treat absent as 'claude_code' via
   *  `accountEngine()` rather than re-deriving the rule per call site. */
  engine?: AgentEngine | null
  /** Enrolment/repair flow to offer: 'chatgpt_device' renders a device-code
   *  panel, 'claude_oauth' the browser flow. */
  userAuthMode?: UserAuthMode | null
  runnerAuthMode?: RunnerAuthMode | null
  /** WHO holds the credential (e.g. 'codex_authority'); never the credential. */
  credentialAuthority?: string | null
  /** Present only for OpenAI accounts. */
  codex?: CodexAccountInfo | null
  usage: AccountUsage | null
  nickname?: string | null
  weeklyReservePct?: number | null
  // Opt-in to spending paid extra-usage credits on this account once it is at
  // its ceiling. Absent means off — spending is never a silent default.
  allowExtraUsage?: boolean
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
  // Emitted by server/sessions/status.py (MID_TURN_STATUSES) but missing here until
  // statusTier's PRESENTATION was typed against this union — the drift ran both ways.
  | 'retrying'
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
  | 'pending_approval'

export type AgentEngine = 'claude_code' | 'codex'

/** The engine the launch UI defaults to, and what an absent `engine` means.
 *
 *  Upgrade safety: every account and every session that predates the engine
 *  registry is Claude Code, and the server applies the same rule
 *  (`server/api/accounts.py:_account_response`). Named once so no picker,
 *  filter or badge re-spells `?? 'claude_code'` and later drifts. */
export const DEFAULT_AGENT_ENGINE: AgentEngine = 'claude_code'

/** Every engine, in the order the Agent selector offers them. */
export const AGENT_ENGINES: readonly AgentEngine[] = ['claude_code', 'codex']

/** Display names for the Agent selector — the ONE place an engine is spelled
 *  for a human, so a card, a filter and a dropdown cannot disagree. */
export const AGENT_ENGINE_LABELS: Record<AgentEngine, string> = {
  claude_code: 'Claude Code',
  codex: 'Codex',
}

export function isAgentEngine(value: unknown): value is AgentEngine {
  return typeof value === 'string' && (AGENT_ENGINES as readonly string[]).includes(value)
}

/** The engine an account can launch; absent/unknown ⇒ Claude Code. */
export function accountEngine(account: Pick<Account, 'engine'>): AgentEngine {
  return isAgentEngine(account.engine) ? account.engine : DEFAULT_AGENT_ENGINE
}

/**
 * One engine's optional control surfaces, as `/api/config` reports them
 * (`server/sessions/engine.py:capabilities_to_wire`). The plan is explicit that
 * CAPABILITIES — not `if (engine === 'codex')` — decide which controls render,
 * so this is the shape every conditional reads from.
 */
export interface AgentCapabilities {
  models: boolean
  reasoningEffort: boolean
  steer: boolean
  interrupt: boolean
  approvals: boolean
  questions: boolean
  fork: boolean
  adopt: boolean
  selfModelSwitch: boolean
  subagents: boolean
  attachments: boolean
  accountRotation: boolean
}

/**
 * What this deployment may currently offer for one engine (`GET /api/config`).
 *
 * `available` is aggregated over the ENABLED fleet hosts: one compatible runner
 * is enough, because dispatch ranks and fails over across the rest.
 * `unavailableReasons` is keyed by host name, so "Codex is greyed out" can be
 * answered without opening Settings › Fleet.
 */
export interface EngineOffer {
  engine: AgentEngine
  available: boolean
  /** Enabled hosts that could run this engine right now. */
  hosts: string[]
  /** Why each OTHER host could not, keyed by host name. Meaningful only while
   *  `blockedReason` is null — when the edge itself blocks the engine, no runner
   *  is at fault and this is `{}`. */
  unavailableReasons: Record<string, string>
  /**
   * An EDGE-side blocker no runner could fix (currently only
   * `authority_unconfigured`: no ChatGPT credential authority, so even a capable
   * runner would produce a session that cannot log in). Null otherwise.
   */
  blockedReason?: string | null
  capabilities: AgentCapabilities
}

/**
 * Statuses a session holds BEFORE its driver exists — parked at the launch-time
 * model gate, or waiting for a capacity slot. No runtime slot, no transcript yet;
 * a WS attach in this state is read-only. Mirrors the server's pre-spawn shapes
 * (`_hold_for_approval` / `_enqueue_session`), which both defer the initial prompt
 * until a LIVE attach — so a view open across the transition must re-attach.
 */
export const PRE_SPAWN_STATUSES: ReadonlySet<SessionStatus> = new Set<SessionStatus>([
  'pending_approval',
  'queued',
])

export function isPreSpawnStatus(status: string | null | undefined): boolean {
  return !!status && PRE_SPAWN_STATUSES.has(status as SessionStatus)
}

// ---------------------------------------------------------------------------
// Session-status vocabularies — the single home for "which statuses count as X"
// (constitution VI). These used to live as three independent inline Sets in
// statusTier.ts, AutomationsTab.tsx and SessionList.tsx, which silently drifted;
// they are DISTINCT concepts (a session can be terminal-for-liveness without
// being bulk-archivable), so they stay separate named sets — but co-located here
// beside PRE_SPAWN_STATUSES so every classification has ONE definition.
//
// 'archived' is a real backend lifecycle status but is deliberately NOT in the
// SessionStatus union (it never arrives on a live session payload), so the sets
// that include it are typed ReadonlySet<string> rather than <SessionStatus>.
// ---------------------------------------------------------------------------

/**
 * Terminal statuses: a session in one of these is truthfully finished WITHOUT a
 * live daemon, so host liveness never demotes it (statusTier.ts). A "Working…"
 * pulse or an "Unreachable" pill over a genuinely-complete session would both be
 * lies — see the liveness demotion in statusPresentation().
 */
export const TERMINAL_STATUSES: ReadonlySet<string> = new Set(['complete', 'ended', 'archived'])

/**
 * Inactive statuses: the session is no longer live ('ended' — process gone;
 * 'archived' — cwd moved out, kept for review). Mirrors the backend's
 * INACTIVE_SESSION_STATUSES (server/db/models.py); used to route a click to the
 * review vs. open handler and to count "active" sessions.
 */
export const INACTIVE_SESSION_STATUSES: ReadonlySet<string> = new Set(['ended', 'archived'])

/**
 * Archivable statuses: a resting, finished session a bulk "Archive done" action
 * may sweep. Narrower than TERMINAL (excludes already-'archived') and includes
 * 'done', which TERMINAL does not (a 'done' session is between turns, not finished).
 */
export const ARCHIVABLE_STATUSES: ReadonlySet<SessionStatus> = new Set<SessionStatus>([
  'done', 'complete', 'ended',
])

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
  engine: AgentEngine
  engineSessionId: string | null
  /** Engine-native reasoning choice; null means the model default. */
  reasoningEffort?: string | null
  /** GitHub nameWithOwner ("owner/name") this session works on, or null for a scratch session. */
  repo?: string | null
  /** Fleet host that ran this session. Null for sessions created before the fleet
   *  existed, and for deployments whose endpoint is not in the host registry. */
  host?: string | null
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
  /** True while a "switch after this turn" model change waits for the in-flight turn to end. */
  modelPending?: boolean
  /** Model id (alias or full id) parked pending human approval; null/absent = no hold. */
  pendingModel?: string | null
  /** The requester's stated reason for pendingModel — a session's own words, or null. */
  pendingModelReason?: string | null
  /** Identity that requested pendingModel; "session:self" when the session asked. */
  pendingModelRequestedBy?: string | null
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
  /** Derived at the edge from the owning host's heartbeat (spec 002 FR-103).
   *  Absent/`fresh` ⇒ no demotion. `unknown` ⇒ the heartbeat snapshot could not be
   *  read (DB down at the edge) — visibly degraded, never silently fresh (NFR-5). */
  liveness?: 'fresh' | 'stale' | 'unreachable' | 'dead' | 'unknown'
  /** ISO8601 of the owning host's last confirmed heartbeat, or null (in-process
   *  sessions and hosts with no heartbeat row). */
  lastConfirmedAliveAt?: string | null
  /** Server-computed: this session was interrupted mid-work by a restart and can be resumed. */
  resumable?: boolean
  /** Pre-restart status when a daemon restart interrupted this session; drives the
   *  orphaned-ask notice. Sent by server/api/sessions.py:_record_to_dict (the browser
   *  serializer — server/sessions/types.py:to_dict is the daemon→edge RPC one). */
  interruptedFromStatus?: string | null
  /** Why a needs_attention/limited session is parked (server pause reason token,
   *  e.g. "all_accounts_busy", "rotation_cap", "pinned_account_limited"). */
  pausedReason?: string | null
  /** ISO instant of the earliest known usage reset for a parked session, or null. */
  pausedUntil?: string | null
  /** Server-derived: parked because no account has usage headroom (or pinned at its
   *  ceiling). Counts toward the "Rotate stalled (N)" button; relabels the pill. */
  usageStalled?: boolean
  /** The automation_id that spawned this session (e.g. 'pr-doctor'); null/absent = human-created. */
  origin?: string | null
  /** The automation's reference key (e.g. 'owner/repo#123'); null/absent when origin is absent. */
  originRef?: string | null
  /** Client-only: true while an optimistic create placeholder awaits the server. Never sent by the server. */
  pending?: boolean
  /** Position in the max-active-sessions queue (1 = next to run); null when not queued. */
  queuePosition?: number | null
  /** Session kind; 'assistant' marks the singleton AI-drawer session, distinct from ordinary work sessions. */
  kind?: 'assistant' | null
}

export interface ArchivedSession {
  id: string
  name: string
  account: string
  cwd: string
  /** GitHub nameWithOwner (e.g. "cairnea/KinDash"); null for scratch/legacy rows. */
  repo: string | null
  engine: AgentEngine
  engineSessionId: string | null
  reasoningEffort?: string | null
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

/**
 * One row of `GET /api/models`, for EITHER engine.
 *
 * The wire shape is a superset, not a union: `{id,label}` is what the Claude
 * catalog has always returned and what every existing caller still gets, and the
 * Codex fields below are simply ABSENT for Claude (never null-filled). One
 * endpoint and one row type is deliberate — two would put an `if (engine ===)`
 * in the picker and guarantee the two lists drift (`server/api/models.py`).
 */
export interface ModelOption {
  id: string
  label: string
  /** Codex: the catalog's own one-line description. */
  description?: string
  /** Codex: this is the account's default model. */
  default?: boolean
  /** Codex: reasoning efforts this model offers. Absent/empty ⇒ no selector. */
  reasoningEfforts?: string[]
  /** Codex: the effort to preselect when this model is chosen. */
  defaultReasoningEffort?: string | null
  /** Codex: e.g. ['text','image']. */
  inputModalities?: string[]
}

export interface PendingDeploy {
  behind: boolean
  /** Sha the RUNNING edge reports on /healthz (short), falling back to the deploy
   *  checkout's HEAD when the edge is unreachable (#859). */
  liveSha: string
  targetSha: string
  /** Deploy checkout HEAD — may be ahead of liveSha after a deploy that advanced the
   *  checkout without restarting the units. Absent from pre-#859 controllers. */
  checkoutSha?: string
  commits: { sha: string; subject: string; author: string; iso: string }[]
  /** What the Deploy button can act on: "green" whenever something pending is
   *  deployable — the tip, or an older green commit when the tip is red/pending. */
  ci: string
  /** The tip's own CI verdict. Absent from pre-green-frontier controllers. */
  tipCi?: string
  /** Newest pending commit with green CI (equals targetSha when the tip is green);
   *  ""/absent when nothing pending is deployable. A partial deploy targets this. */
  deployableSha?: string
  /** How many of the pending commits a deploy of deployableSha ships. */
  deployableCount?: number
}

export interface DeployHostProgress {
  state: 'pending' | 'running' | 'done' | 'failed' | 'offline' | 'disabled' | 'unconfigured'
  step: string | null
  error: string | null
  sha: string | null
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
  // `replayed` = mid-turn sessions whose interrupted prompt was re-issued after
  // the restart (issue #864) — a subset of `restored`.
  restore?: { restored?: string[]; failed: string[]; gaveUp?: string[]; replayed?: string[] }
  // Per-fleet-runner progress during the `runners` phase, keyed by host name.
  // Absent/empty when the deploy was a no-op (already up to date).
  hosts?: Record<string, DeployHostProgress>
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
  /** Seconds since the last successful /info probe; null if never probed. Issue #766:
   *  a non-null value > stale_after_s means the daemon is unreachable (available=false). */
  lastSeenAgoSec: number | null
}

export interface ApiError {
  status: number
  error: { code: string; message: string }
}

export interface AllLimitedError extends ApiError {
  earliestReset: string | null
}

/**
 * A device-code login, for EVERY provider that uses one.
 *
 * Was `CopilotAddState`. GitHub Copilot and OpenAI/ChatGPT run the identical
 * state machine and the backend spells them identically on purpose
 * (`server/codex/auth_types.py:LoginSnapshot.to_wire` vs the Copilot add
 * routes), so a second provider-named copy of this interface would be the
 * parallel-implementation shape this repo bans — and would invite a second
 * device-code panel to drift from the first.
 *
 * `userCode` / `verificationUri` are the ONLY login details allowed out of the
 * authority: single-use, short-lived and useless without the operator's own
 * session. They must never be persisted, cached to storage, or put in an error
 * or analytics payload.
 */
export interface DeviceCodeState {
  state: 'pending' | 'awaiting_authorization' | 'authorized' | 'failed'
  userCode: string | null
  verificationUri: string | null
  /** Why a `failed` login failed, already scrubbed server-side. */
  error?: string | null
  /** Bumps per login attempt; lets a late poll be recognised as stale. */
  generation?: number | null
}

export interface McpAuthState {
  state: 'idle' | 'starting' | 'awaiting_redirect' | 'exchanging' | 'success' | 'error' | 'cancelled'
  server?: string | null
  message?: string | null
  authUrl?: string | null
  accounts: string[]
  startedAt?: number | null
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
  /** Action to take when a model requires approval: 'approve' | 'deny' | null (ask). */
  modelApprovalAction: string | null
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
