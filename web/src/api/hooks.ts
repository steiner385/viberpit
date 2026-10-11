import { useQuery, useMutation, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState, useRef, useCallback, useSyncExternalStore } from 'react'
import { api, ApiRequestError } from './client'
import {
  fetchAutomations, fetchAutomationEvents, fetchSpawnedSessions, patchAutomation,
  fetchAutomationActionable, postAutomationTrigger,
  fetchConsolidation, postConsolidate, fetchBacklogGroups,
  fetchBacklogCandidates, postDispatchGroup,
  fetchInboxClusters, postClusterDraft, postClusterFile, postClusterDismiss, postClusterAttach,
  postClusterRestore,
  type InboxClusters, type ClusterDraft, type ClusterAttachTarget,
  type Automation, type AutomationEvent, type AutomationPatch,
  type ActionableItem, type TriggerResult, type ConsolidationView,
  type BacklogGroupsView, type BacklogCandidatesView, type DispatchGroupResponse,
  type ConfigField,
} from './automations'
import { DEFAULT_AGENT_ENGINE } from './types'
import type {
  McpStatusRow, McpServerState, Account, Session, ReauthState, ArchivedSession, AdoptionCandidate, AdoptOutcome, ModelOption, BatchResult,
  DeployStatusResponse, PendingDeploy, DeployProgress, PipelinePr, StageModel, CheckInsights,
  DaemonHealth, DeviceCodeState, McpAuthState, AgentEngine, AgentCapabilities, EngineOffer,
  LibraryPluginDetail, LibraryPluginsResponse, LibrarySkill, LibraryCommand,
  LibraryMcpServer, McpServerConfig, LibraryContent,
  SessionPreset, PromptTemplate, PermissionProfile, AssessResponse, RuleWire,
} from './types'
import { fetchDeployStatus, startDeploy } from './deploy'
import {
  fetchInitiative, advanceInitiative, stepBackInitiative, unblockInitiative, decomposeAgain,
  createInitiative,
  updateInitiative,
  adoptArtifact,
  fetchMaterialization, acceptMaterialization, rejectMaterialization,
  fetchIncidents, actOnIncident,
  fetchGates, type GateRegistry,
  type Initiative, type CreateInitiativeInput, type UpdateInitiativeInput,
  type InitiativeArtifact, type AdoptArtifactInput,
  type MaterializationProposal, type MaterializationApplied,
  type Incident, type IncidentActionInput,
} from './sdlc'
import {
  createProject, updateProject, requestProjectSync,
  fetchOnboarding, proposeMap, proposeConstitution, runClassify, advanceMap, advanceLive,
  removeDependency, createEdge, deleteEdge,
  editMapChanges, abandonMapChanges, rebaseMapChanges, retireCapability, mergeCapabilities,
  fetchPolicySchema, updateProductPolicy, type ProductPolicyPatch, fetchProductPolicyCoverage, fetchProductImplementationPolicy,
  fetchProjectHealth,
  fetchOutcomes, fetchWorkItemIssue, dispositionIssue, releaseHold, approveEscalation,
  type WorkItemIssue, type IssueDispositionInput, type IssueDispositionResult, type HoldReleaseResult,
  type ApproveEscalationResult,
  type Project, type CreateProjectInput, type UpdateProjectInput, type OnboardingView,
  type RemoveDependencyInput, type RemoveDependencyResult,
  type CreateEdgeInput, type ProjectEdgeInfo,
  type MapChangeInput, type MapChangeResult, type AbandonResult, type RetireCapabilityInput, type RetireCapabilityResult, type MergeCapabilitiesInput, type MergeCapabilityResult,
  type ProjectHealth,
  type DispatchOutcome,
} from './pm'
import {
  fetchRoadmapMap, fetchLeaves, fetchQueue, fetchHolds, fetchProject,
  type RoadmapMap, type Leaf, type LeavesScope, type QueueView, type HoldsView, type MapChangesPointer,
} from './roadmap'
import { fetchCapacity, fetchCapacityBinding, type CapacityReadout } from './capacity'
import { FLEET_HOSTS_KEY, type FleetHost } from './fleet'
import {
  fetchTriage, acceptProposal, rejectProposal, acceptBulk, rejectBulk, updateOverlay, undoProposal,
  dismissProposal, dismissBulk,
  fetchDecisions, acceptDecision, rejectDecision,
  type TriageProposal, type OverlayEdits, type AcceptBulkResult, type BulkSweepRow,
  type WorkItemOverlay, type UndoResult,
  type DecisionProposal, type DecisionResult,
} from './triage'
import { useSessionSocket } from '@/ws/client'
import { extractMutationErrorMessage } from './queryClient'
import type { SessionEvent } from '@/session/events'
import type { ToolDetailMode } from '@/session/groupBlocks'
import { getPendingSessions, subscribePending, addPendingSession, removePendingSession } from '@/lib/pendingSessions'
import { reconcileFlight } from '@/lib/promptFlight'
import { isSessionsViewing, setSessionsViewing } from './sessionsViewing'
import { reconcileSessions } from '@/session/reconcile'
import { toast } from 'sonner'

// Query keys
export const QK = {
  accounts: ['accounts'] as const,
  // Engine- AND account-scoped. Codex's list comes from the selected account's
  // own app-server and differs by plan, so a single ['models'] key would serve
  // one engine's catalogue to the other engine's picker the moment both are
  // offered — and a Codex account switch would show the previous account's models.
  models: (engine: AgentEngine = DEFAULT_AGENT_ENGINE, account?: string | null) =>
    ['models', engine, account ?? null] as const,
  sessions: ['sessions'] as const,
  archived: ['archived-sessions'] as const,
  adoptable: ['adoptable-sessions'] as const,
  config: ['config'] as const,
  githubRepos: ['github-repos'] as const,
  settings: ['settings'] as const,
  sessionSearch: (q: string, scope: string = 'live') => ['session-search', scope, q] as const,
  prs: ['prs'] as const,
  pipelineRepos: ['pipeline-repos'] as const,
  automations: ['automations'] as const,
  automationEvents: (id: string) => ['automation-events', id] as const,
  automationSessions: (id: string) => ['automation-sessions', id] as const,
  pipelineInsights: (repo?: string, window?: string) =>
    ['pipeline-insights', repo, window] as const,
  automationActionable: (id: string) => ['automation-actionable', id] as const,
  inboxClusters: (id: string) => ['automation-inbox-clusters', id] as const,
  inboxDraft: (id: string, ids: string[]) => ['automation-inbox-draft', id, ids] as const,
  consolidation: ['automation-consolidation'] as const,
  backlogGroups: ['backlog-groups'] as const,
  backlogCandidates: ['backlog-candidates'] as const,
  daemonHealth: ['daemon-health'] as const,
  copilotAddStatus: (name: string) => ['copilot-add-status', name] as const,
  openaiAddStatus: (name: string) => ['openai-add-status', name] as const,
  openaiReauthStatus: (name: string) => ['openai-reauth-status', name] as const,
  libraryPlugins: ['library', 'plugins'] as const,
  libraryPlugin: (id: string) => ['library', 'plugin', id] as const,
  librarySkills: ['library', 'skills'] as const,
  libraryCommands: ['library', 'commands'] as const,
  libraryMcp: ['library', 'mcp'] as const,
  mcpScope: ['library', 'mcp', 'scope'] as const,
  libraryContent: (kind: string, id: string, plugin?: string) =>
    ['library', 'content', kind, id, plugin ?? null] as const,
  initiative: (slug: string) => ['sdlc', 'initiative', slug] as const,
  // Deliberately EXTENDS `initiative`: the proposal and the detail are read
  // together on the Track tab and change together, so react-query's prefix
  // matching lets one `QK.initiative(slug)` invalidation refresh both.
  materialization: (slug: string) => ['sdlc', 'initiative', slug, 'materialization'] as const,
  // Control-band INCIDENTS (#804) — deliberately not `triage` below, which is the
  // PM classification inbox. Same word, unrelated data.
  incidents: ['sdlc', 'incidents'] as const,
  gates: ['sdlc', 'gates'] as const,
  triage: ['pm', 'triage'] as const,          // phase 2 OWNS this key
  // The inbox's SECOND card kind (spec 2026-09-18 §6). A key of its own, not a
  // widening of `triage`: the two come from two endpoints with two resolution
  // routes, and one key would make every decision accept refetch the whole
  // classification queue (and the reverse).
  decisions: ['pm', 'decisions'] as const,
  onboarding: (slug: string) => ['pm', 'onboarding', slug] as const,
  roadmapMap: ['pm', 'map'] as const,
  leaves: (scope: string) => ['pm', 'leaves', scope] as const,
  queue: ['pm', 'queue'] as const,
  holds: ['pm', 'holds'] as const,
  workItemIssue: (id: string) => ['pm', 'issue', id] as const,
  project: (slug: string) => ['pm', 'project', slug] as const,
  policySchema: ['pm', 'policy-schema'] as const,
  implementationPolicy: (slug: string) => ['pm', 'project', slug, 'implementation-policy'] as const,
  policyCoverage: (slug: string) => ['pm', 'project', slug, 'policy-coverage'] as const,
  health: (slug: string) => ['pm', 'health', slug] as const,
  outcomes: (workItemId: string) => ['pm', 'outcomes', workItemId] as const,
  capacity: ['capacity'] as const,
}

/**
 * The model list for one engine, and — for Codex — for one account.
 *
 * Claude's catalogue is curated and host-independent, so the URL stays the bare
 * `/models` every existing caller already sends (the route defaults `engine` for
 * exactly that reason). Codex's comes from the account's own app-server and
 * differs by plan, which is why `account` is required there: asking without one
 * is a 400 `account_required`, so the query simply does not run and the picker
 * says "select an account" instead.
 */
export function useModels(
  engine: AgentEngine = DEFAULT_AGENT_ENGINE,
  account?: string | null,
) {
  const needsAccount = engine !== DEFAULT_AGENT_ENGINE
  return useQuery<ModelOption[]>({
    queryKey: QK.models(engine, account),
    queryFn: () => api.get<ModelOption[]>(
      needsAccount
        ? `/models?engine=${encodeURIComponent(engine)}&account=${encodeURIComponent(account ?? '')}`
        : '/models',
    ),
    enabled: !needsAccount || !!account,
    staleTime: 5 * 60_000,   // curated list rarely changes
  })
}

/** Issue #434: poll daemon lifecycle health for the dashboard indicator + restart toast.
 *  Cheap payload; 15s cadence matches the server-side poll granularity. */
export function useDaemonHealth() {
  return useQuery<DaemonHealth>({
    queryKey: QK.daemonHealth,
    queryFn: () => api.get<DaemonHealth>('/daemon/health'),
    staleTime: 10_000,
    refetchInterval: 15_000,
  })
}

export function useAccounts() {
  return useQuery<Account[]>({
    queryKey: QK.accounts,
    queryFn: () => api.get<Account[]>('/accounts'),
    staleTime: 25_000,
    refetchInterval: 30_000,
  })
}

export function useSessions() {
  const pending = useSyncExternalStore(subscribePending, getPendingSessions, getPendingSessions)
  const query = useQuery<Session[]>({
    queryKey: QK.sessions,
    queryFn: () => api.get<Session[]>(isSessionsViewing() ? '/sessions?viewing=1' : '/sessions'),
    staleTime: 3_000,
    refetchInterval: 5_000,
    // Version-guarded merge: a stale in-flight poll must not overwrite a session the WS
    // patched to a newer revAt. reconcileSessions keeps the newer of cache vs poll per id.
    structuralSharing: (prev, next) => reconcileSessions(prev as Session[] | undefined, next as Session[]),
  })
  // The poll is the durable second signal for the "Sent — starting…" list pill: a
  // socket that was down while a turn ran never sees it start, and the resting
  // status it sees afterwards is (rightly) not a clear on its own. The row's
  // hand-back clock is — see lib/promptFlight. `query.data` is reference-stable
  // across unchanged polls (reconcileSessions), so this is a no-op most ticks.
  useEffect(() => {
    if (query.data) reconcileFlight(query.data)
  }, [query.data])
  const data = useMemo<Session[] | undefined>(() => {
    if (pending.length === 0) return query.data
    return [...(query.data ?? []), ...pending]
  }, [query.data, pending])
  return { ...query, data }
}

/** Tell the server to (re)generate summaries only while the sessions list is on-screen. */
export function useSessionsViewing(active: boolean): void {
  useEffect(() => {
    setSessionsViewing(active)
    return () => setSessionsViewing(false)
  }, [active])
}

export interface GithubRepo {
  nameWithOwner: string
  name: string
  owner: string
  isPrivate: boolean
  url: string
  defaultBranch: string | null
  pushedAt: string | null
  cloned: boolean
}

export interface ConfigInfo {
  repoRoot: string
  adoptionEnabled: boolean
  /** Edge runs the server-side dictation engine (`VIBERPIT_ENABLE_STT` + `stt` extra). */
  sttEnabled?: boolean
  /**
   * Edge accepts field Core Web Vitals (`VIBERPIT_WEB_VITALS_ENABLED`, #1411).
   * Absent on an edge that predates the endpoint — treated as OFF, so an old
   * server never receives a POST it would 404 on every page load.
   */
  webVitalsEnabled?: boolean
  /**
   * Per-engine offerability + capabilities. Absent on an edge that predates the
   * engine registry — treat that as "Claude Code only", which is what
   * `useEngineOffers()` degrades to.
   */
  engines?: EngineOffer[]
}

export function useConfig() {
  return useQuery<ConfigInfo>({
    queryKey: QK.config,
    queryFn: () => api.get<ConfigInfo>('/config'),
    staleTime: 60_000,
  })
}

/** Never a new array identity for the same "no offers" answer — this is a
 *  dependency of the launch UI's memoised engine resolution. */
const NO_ENGINE_OFFERS: EngineOffer[] = []

/**
 * What engines this deployment offers, read off the ONE `/config` query.
 *
 * Deliberately a selector over `useConfig()` rather than a second hook with its
 * own key: `/api/config` already carries `engines`, and a parallel query would
 * be a second source of truth for "may the user pick Codex?" that could answer
 * differently mid-session.
 */
export function useEngineOffers(): EngineOffer[] {
  const { data } = useConfig()
  return data?.engines ?? NO_ENGINE_OFFERS
}

/**
 * The server-advertised control surface of one engine, or undefined when
 * `/api/config` has not answered (or predates the engine registry).
 *
 * Lives here rather than beside its first consumer so EVERY capability-gated
 * surface reads one hook: a component-file copy cannot be imported without
 * tripping `react-refresh/only-export-components`, which is exactly how a
 * second `offers.find(...)` gets written somewhere else.
 *
 * Callers must treat `undefined` as "capability present" — see
 * `canUseEngine` in `@/session/engineCapabilities`.
 */
export function useEngineCapabilities(
  engine: AgentEngine | undefined,
): AgentCapabilities | undefined {
  const offers = useEngineOffers()
  return offers.find((o) => o.engine === (engine ?? DEFAULT_AGENT_ENGINE))?.capabilities
}

// Self-heal for the New Session repo dropdown: a fetch that fails while the
// edge/daemon is still booting after a restart used to strand the dropdown on
// "— could not load repos —" until a manual reload. Poll ONLY while errored —
// a healthy query keeps the ordinary staleTime/refetch-on-focus behavior, so
// this can never turn into a background request storm.
export const REPOS_ERROR_REFETCH_MS = 15_000

export function reposRefetchInterval(
  query: { state: { status: 'pending' | 'error' | 'success' } },
): number | false {
  return query.state.status === 'error' ? REPOS_ERROR_REFETCH_MS : false
}

export function useGithubRepos() {
  return useQuery<GithubRepo[]>({
    queryKey: QK.githubRepos,
    queryFn: () => api.get<GithubRepo[]>('/github/repos'),
    staleTime: 30_000,
    // The edge serves a last-good cached list on GitHub failure, so this rarely errors;
    // bound retries anyway (default is 3) so a genuine error can't compound across
    // refetch-on-focus and multiple tabs into a request storm.
    retry: 1,
    refetchInterval: reposRefetchInterval,
  })
}

/** One read-only env flag shown on Settings › About. The server sends an allowlist. */
export interface ManagedFlag {
  key: string
  value: string | number | boolean | null
  description: string
}

/** The effective global cap and its derivation (spec 2026-09-20 D3). */
export interface GlobalCapDefaults {
  /** Σ host caps over enabled hosts; null = unbounded (no hosts, or an enabled host has no cap). */
  physical: number | null
  /** AppSettings.maxActiveSessions as stored; null = no attention ceiling. */
  attention: number | null
  /** min(physical, attention), or the server default (5) when both are unbounded. Always ≥ 1. */
  effective: number
  binding: 'physical' | 'attention' | 'default'
  hosts: { name: string; cap: number | null }[]
}

/** What the server falls back to when the matching AppSettings column is NULL. */
export interface EffectiveDefaults {
  repoRoot: string
  archiveHome: string | null
  modelApprovalAction: string
  modelApprovalDenylist: string
  globalCap: GlobalCapDefaults
}

export interface AppSettings {
  defaultPermissionMode: string
  thinkingCollapsed: boolean
  repoRoot: string | null
  defaultToolDetail: ToolDetailMode
  // Cap on retained archived sessions; null = unlimited. On PUT, send 0 to clear to
  // unlimited (the backend treats <=0 as null) — never send null.
  maxArchivedSessions: number | null
  // Archive home path; null = use server default. On PUT, send "" to clear to default.
  archiveHome: string | null
  // The ATTENTION ceiling (spec 2026-09-20 D3); null = none. The effective cap is effectiveDefaults.globalCap.
  maxActiveSessions: number | null
  // App-wide toggle: when false, completed sessions are not auto-archived (they linger
  // like a per-session manual opt-out). The server's NULL default surfaces here as true.
  autoArchiveEnabled: boolean
  // Global model-approval policy: 'require_approval' | 'block' | 'allow' | null (server default).
  modelApprovalAction: string | null
  // Comma-separated model ids/tiers that require approval; null = server default (fable).
  modelApprovalDenylist: string | null
  // AI assistant (spec §4.11). null = server env default (VIBERPIT_ASSISTANT_ENABLED /
  // VIBERPIT_ASSISTANT_MODEL) for both.
  assistantEnabled?: boolean | null
  assistantModel?: string | null
  // Operator tier (v2 spec B1): deploy start, host upgrade, account refresh/enable/reauth.
  // null = server env default (VIBERPIT_ASSISTANT_OPERATOR_TOOLS), which is OFF.
  assistantOperatorTools?: boolean | null
  /** The modes PUT accepts, in display order. The client's PERMISSION_MODES must equal this set. */
  acceptedPermissionModes: string[]
  effectiveDefaults: EffectiveDefaults
  managed: ManagedFlag[]
  about: { gitSha: string | null; hostName: string }
}

export function useSettings() {
  return useQuery<AppSettings>({
    queryKey: QK.settings,
    queryFn: () => api.get<AppSettings>('/settings'),
    staleTime: 30_000,
  })
}

// Every mutation that moves a capacity dial (settings, hosts, accounts) also
// invalidates QK.capacity: the Delivery → Capacity stack and the Limits
// section's readout card otherwise show the old ceiling for up to 30 s
// (spec 2026-09-20 C20, D10).
export function useUpdateSettings() {
  const qc = useQueryClient()
  return useMutation<AppSettings, ApiRequestError, Partial<AppSettings>>({
    mutationFn: (body) => api.put<AppSettings>('/settings', body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.settings })
      qc.invalidateQueries({ queryKey: QK.capacity })
    },
  })
}

export interface CreateSessionInput {
  name?: string
  account?: string
  initialPrompt?: string
  repo?: string
  branch?: string
  permissionMode?: string
  attachmentIds?: string[]
  model?: string
  /** Which agent runs the session; omitted ⇒ the server's default (Claude Code). */
  engine?: AgentEngine
  reasoningEffort?: string | null
  /** Optional runner preference. null → auto-rank; a name → that host first. */
  host?: string | null
  /** Force where the GUI renders; omitted/'auto' → decided from client locality. */
  display?: 'auto' | 'native' | 'virtual'
  presetId?: string
  permissionProfileId?: string
  /** Per-session opt-outs. Omitted/true ⇒ governed by the global autopilot mode /
   *  auto-archive setting; false ⇒ this session is pinned to manual / never
   *  auto-archived (the server maps them onto `autopilotPaused` and
   *  `autoArchiveOptOut`). */
  autopilot?: boolean
  autoArchive?: boolean
}

export function useCreateSession() {
  const qc = useQueryClient()
  return useMutation<Session, ApiRequestError, CreateSessionInput, { tempId: string }>({
    mutationFn: (input) => api.post<Session>('/sessions', input),
    // The one caller (PromptDock → DispatchBar) renders the failure inline in
    // the bar that raised it; the global toast would be a second, vaguer copy of
    // the same message. It was two callers until the New Session dialog was
    // retired — with the dialog gone, the bar's inline error is the user's ONLY
    // copy, which is what makes suppressing the toast a real commitment rather
    // than a de-duplication.
    meta: { suppressErrorToast: true },
    onMutate: (input) => {
      // Placeholder lives outside the query cache so the 5s poll can't wipe it
      // while the backend spins up the claude PTY.
      const tempId = addPendingSession({
        name: input.name?.trim() || input.initialPrompt?.trim().slice(0, 60) || 'New session',
        accountName: input.account ?? '',
        // The REAL engine, so a Codex card is never briefly labelled Claude while
        // the daemon spins the runtime up.
        engine: input.engine ?? DEFAULT_AGENT_ENGINE,
        engineSessionId: null,
        status: 'in_progress',
        createdAt: new Date().toISOString(),
        repo: input.repo ?? null,
        prs: [],
      })
      return { tempId }
    },
    onSuccess: (session) => {
      // Inject the real card before the placeholder is removed (onSettled) to avoid a flicker.
      qc.setQueryData<Session[]>(QK.sessions, (prev) => {
        const list = prev ?? []
        if (list.some((s) => s.id === session.id)) return list
        return [...list, session]
      })
      qc.invalidateQueries({ queryKey: QK.sessions })
    },
    onSettled: (_data, _err, _input, ctx) => {
      if (ctx?.tempId) removePendingSession(ctx.tempId)
    },
  })
}

export function useRenameSession() {
  const qc = useQueryClient()
  return useMutation<Session, ApiRequestError, { id: string; name: string }>({
    mutationFn: ({ id, name }) => api.patch<Session>(`/sessions/${id}`, { name }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.sessions }) },
  })
}

/** Stop ONE running Task sub-agent (POST /sessions/{id}/agents/{toolUseId}/stop). The
 *  CLI answers on the event stream (subAgentEnded), so nothing is invalidated here. */
export function useStopSubAgent() {
  return useMutation<{ ok: boolean; taskId: string }, ApiRequestError, { id: string; toolUseId: string }>({
    mutationFn: ({ id, toolUseId }) =>
      api.post<{ ok: boolean; taskId: string }>(`/sessions/${id}/agents/${encodeURIComponent(toolUseId)}/stop`, {}),
  })
}

export function useSendPrompt() {
  const qc = useQueryClient()
  return useMutation<Session, ApiRequestError, { id: string; text: string; replySource?: 'suggested' }>({
    mutationFn: ({ id, text, replySource }) => api.post<Session>(`/sessions/${id}/prompt`, { text, ...(replySource ? { replySource } : {}) }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.sessions }) },
  })
}

export function useSetAutopilotPaused() {
  const qc = useQueryClient()
  return useMutation<Session, ApiRequestError, { id: string; paused: boolean }>({
    mutationFn: ({ id, paused }) => api.post<Session>(`/sessions/${id}/autopilot`, { paused }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.sessions }) },
  })
}

export function useSetAutoArchiveOptOut() {
  const qc = useQueryClient()
  return useMutation<Session, ApiRequestError, { id: string; optedOut: boolean }>({
    mutationFn: ({ id, optedOut }) => api.post<Session>(`/sessions/${id}/auto-archive`, { optedOut }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.sessions }) },
  })
}

/** The user's answer to a "likely complete" hand-back: confirm archives, reject
 *  clears the hint. Invalidates the list either way — confirm removes the card,
 *  reject drops the pill. */
export function useResolveSuspectedComplete() {
  const qc = useQueryClient()
  return useMutation<{ decision: 'confirm' | 'reject'; sessionId: string }, ApiRequestError,
                     { id: string; decision: 'confirm' | 'reject' }>({
    mutationFn: ({ id, decision }) =>
      api.post<{ decision: 'confirm' | 'reject'; sessionId: string }>(`/sessions/${id}/suspected-complete`, { decision }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.sessions }) },
  })
}

export function useQueueMove() {
  const qc = useQueryClient()
  return useMutation<Session, ApiRequestError, { id: string; op: 'up' | 'down' | 'top' | 'position'; position?: number }>({
    mutationFn: ({ id, op, position }) => api.post<Session>(`/sessions/${id}/queue-move`, { op, position }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.sessions }) },
  })
}

export function useQueueCancel() {
  const qc = useQueryClient()
  return useMutation<void, ApiRequestError, { id: string }>({
    mutationFn: ({ id }) => api.del<void>(`/sessions/${id}/queue`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.sessions }) },
  })
}

export function useQueueStartNow() {
  const qc = useQueryClient()
  return useMutation<{ ok: boolean; status: string }, ApiRequestError, { id: string }>({
    mutationFn: ({ id }) => api.post<{ ok: boolean; status: string }>(`/sessions/${id}/start-now`, {}),
    onSettled: () => { qc.invalidateQueries({ queryKey: QK.sessions }) },
  })
}

export function useModelApproval() {
  const qc = useQueryClient()
  return useMutation<Session, ApiRequestError, { id: string; decision: 'approve' | 'deny'; model?: string }>({
    mutationFn: ({ id, decision, model }) =>
      api.post<Session>(`/sessions/${id}/model-approval`, { decision, model }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.sessions }) },
  })
}

// "Reload session": rebuild the driver in place (same account, transcript
// resumed on a fresh CLI). The caller re-attaches the socket on success so the
// transcript is rebuilt from the daemon's state rather than the stale one the
// user was staring at.
export function useReloadSession() {
  const qc = useQueryClient()
  return useMutation<Session, ApiRequestError, { id: string }>({
    mutationFn: ({ id }) => api.post<Session>(`/sessions/${id}/reload`, {}),
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.sessions }) },
  })
}

// "Next best account": the daemon moves the session to the account the selector
// ranks best and continues the conversation (replays a stalled prompt). The caller
// re-attaches the socket on success, exactly as reload does.
export function useRotateSession() {
  const qc = useQueryClient()
  return useMutation<Session, ApiRequestError, { id: string }>({
    mutationFn: ({ id }) => api.post<Session>(`/sessions/${id}/rotate`, {}),
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.sessions }) },
    onError: (e) => {
      const code = (e.body.detail as { code?: string } | null)?.code
      toast.error(code === 'skipped:all_accounts_busy' ? 'No account has headroom right now' : `Couldn't rotate session`)
    },
  })
}

// Pin the session to its current account (usage-ceiling rotation then parks it as
// `limited` instead of moving it) or clear the pin; unpinning a limited session
// rotates it immediately on the server.
export function useSetAccountPin() {
  const qc = useQueryClient()
  return useMutation<Session, ApiRequestError, { id: string; pinned: boolean }>({
    mutationFn: ({ id, pinned }) => api.post<Session>(`/sessions/${id}/account-pin`, { pinned }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.sessions }) },
  })
}

export function useFixPr() {
  const qc = useQueryClient()
  return useMutation<Session, ApiRequestError, { id: string; number: number }>({
    mutationFn: ({ id, number }) => api.post<Session>(`/sessions/${id}/fix-pr`, { number }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.sessions }) },
  })
}

// Sparkle (✨) on-demand generation: ALWAYS asks the backend LLM for fresh
// next-prompt suggestions (never a cached value or a hard-coded fallback — those
// live only in the dropdown). Resolves to the suggestion strings; [] when the
// model yields nothing.
export function useGenerateSuggestions() {
  return useMutation<string[], ApiRequestError, { id: string }>({
    mutationFn: async ({ id }) => {
      const r = await api.post<{ suggestions: string[] }>(`/sessions/${id}/suggest`, {})
      return r.suggestions ?? []
    },
  })
}

export function useCloseSession() {
  const qc = useQueryClient()
  return useMutation<void, ApiRequestError, string, { prev?: Session[]; removed?: Session }>({
    mutationFn: (id) => api.del<void>(`/sessions/${id}`),
    onMutate: async (id) => {
      await qc.cancelQueries({ queryKey: QK.sessions })
      const prev = qc.getQueryData<Session[]>(QK.sessions)
      const removed = prev?.find((x) => x.id === id)
      qc.setQueryData<Session[]>(QK.sessions, (cur) => (cur ?? []).filter((x) => x.id !== id))
      return { prev, removed }
    },
    onError: (_e, _id, ctx) => {
      // Restore the optimistic removal so the UI doesn't show a gap.
      // Toast is deferred to onSettled, which refetches to confirm real state —
      // a 503/409 from the daemon often means the archive succeeded but the
      // response was lost; toasting here would be a false failure.
      if (ctx?.prev) qc.setQueryData(QK.sessions, ctx.prev)
    },
    onSuccess: (_data, id, ctx) => {
      toast.success(`Archived ${ctx?.removed?.name ?? 'session'}`, {
        duration: 8000,
        action: {
          label: 'Undo',
          onClick: () => {
            void unarchiveRequest(id).then(() => {
              qc.invalidateQueries({ queryKey: QK.sessions })
              qc.invalidateQueries({ queryKey: QK.archived })
            })
          },
        },
      })
    },
    onSettled: async (_data, error, id, ctx) => {
      if (!error) {
        qc.invalidateQueries({ queryKey: QK.sessions })
        qc.invalidateQueries({ queryKey: QK.archived })
        return
      }
      // Force-refetch to discover whether the archive actually took effect
      // despite the error response (e.g. daemon wedge that resolved mid-flight).
      await qc.refetchQueries({ queryKey: QK.sessions })
      await qc.refetchQueries({ queryKey: QK.archived })
      const current = qc.getQueryData<Session[]>(QK.sessions)
      const stillLive = current?.some((s) => s.id === id) ?? false
      if (!stillLive) {
        // Archive succeeded — daemon lost the response. Show success with undo.
        toast.success(`Archived ${ctx?.removed?.name ?? 'session'}`, {
          duration: 8000,
          action: {
            label: 'Undo',
            onClick: () => {
              void unarchiveRequest(id).then(() => {
                qc.invalidateQueries({ queryKey: QK.sessions })
                qc.invalidateQueries({ queryKey: QK.archived })
              })
            },
          },
        })
      } else {
        toast.error(`Couldn't archive ${ctx?.removed?.name ?? 'session'}`)
      }
    },
  })
}

export function useRefreshAccount() {
  const qc = useQueryClient()
  return useMutation<Account, ApiRequestError, string>({
    mutationFn: (name) => api.post<Account>(`/accounts/${encodeURIComponent(name)}/refresh`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.accounts }) },
  })
}

export interface AccountUpdate {
  name: string
  nickname?: string | null
  weeklyReservePct?: number | null
  /** Per-account model-approval override: 'require_approval' | 'block' | 'allow' | null (inherit global). */
  modelApprovalAction?: string | null
  allowExtraUsage?: boolean
}

export function useUpdateAccount() {
  const qc = useQueryClient()
  return useMutation<Account, ApiRequestError, AccountUpdate>({
    mutationFn: ({ name, ...fields }) =>
      api.patch<Account>(`/accounts/${encodeURIComponent(name)}`, fields),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.accounts })
      qc.invalidateQueries({ queryKey: QK.capacity })
    },
  })
}

export function useSetAccountEnabled() {
  const qc = useQueryClient()
  return useMutation<Account, ApiRequestError, { name: string; enabled: boolean }>({
    mutationFn: ({ name, enabled }) =>
      api.patch<Account>(`/accounts/${encodeURIComponent(name)}/enabled`, { enabled }),
    // Refetch accounts so the dashboard + pickers reflect the new state at once.
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.accounts })
      qc.invalidateQueries({ queryKey: QK.capacity })
    },
  })
}

// Always the in-process PKCE/setup-token exchange (issue #1130) — no browser,
// no virtual display. The account already has a slot, so there is no mode to pick.
export function useReauthAccount() {
  return useMutation<ReauthState, ApiRequestError, string>({
    mutationFn: (name) => api.post<ReauthState>(`/accounts/${encodeURIComponent(name)}/reauth`),
  })
}

export function useSubmitReauthCode() {
  return useMutation<ReauthState, ApiRequestError, { name: string; code: string }>({
    mutationFn: ({ name, code }) =>
      api.post<ReauthState>(`/accounts/${encodeURIComponent(name)}/reauth/code`, { code }),
  })
}

export function useReauthStatus(name: string, enabled: boolean) {
  return useQuery<ReauthState>({
    queryKey: ['reauth-status', name],
    queryFn: () => api.get<ReauthState>(`/accounts/${encodeURIComponent(name)}/reauth`),
    enabled,
    // Poll while a reauth is in flight; stop automatically once the status
    // reaches a terminal state. Keeping the polling-stop here (instead of a
    // setState-in-effect in the component) lets the last status stay visible.
    refetchInterval: (query) => {
      const s = query.state.data?.state
      return s === 'success' || s === 'error' || s === 'timeout' ? false : 1500
    },
    refetchOnWindowFocus: false,
    staleTime: 0,
  })
}

export function useCancelReauth() {
  return useMutation<ReauthState, ApiRequestError, string>({
    mutationFn: (name) => api.post<ReauthState>(`/accounts/${encodeURIComponent(name)}/reauth/cancel`),
    // fire-and-forget cancel: never toast on error (see queryClient global handler)
    meta: { suppressErrorToast: true },
  })
}

export function useDeleteAccount() {
  const qc = useQueryClient()
  return useMutation<void, ApiRequestError, string>({
    mutationFn: (name) => api.del<void>(`/accounts/${encodeURIComponent(name)}`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.accounts }) },
  })
}

// Add-account reuses the interactive browser-OAuth login (same global state
// machine as reauth) to create a brand-new account slot. The name is sent in
// the body since the slot does not exist yet.
export function useAddAccount() {
  return useMutation<ReauthState, ApiRequestError, string>({
    mutationFn: (name) => api.post<ReauthState>('/accounts/add', { name }),
  })
}

export function useAddAccountStatus(enabled: boolean) {
  return useQuery<ReauthState>({
    queryKey: ['add-account-status'],
    queryFn: () => api.get<ReauthState>('/accounts/add'),
    enabled,
    // Poll while the login is in flight; stop once a terminal state is reached.
    refetchInterval: (query) => {
      const s = query.state.data?.state
      return s === 'success' || s === 'error' || s === 'timeout' ? false : 1500
    },
    refetchOnWindowFocus: false,
    staleTime: 0,
  })
}

export function useCancelAddAccount() {
  return useMutation<ReauthState, ApiRequestError, void>({
    mutationFn: () => api.post<ReauthState>('/accounts/add/cancel'),
    // fire-and-forget cancel: never toast on error (see queryClient global handler)
    meta: { suppressErrorToast: true },
  })
}

// Copilot device-flow add: initiates the GitHub device-flow OAuth for a new
// Copilot account slot. The name is sent in the body since the slot does not exist yet.
export function useAddCopilotAccount() {
  const qc = useQueryClient()
  return useMutation<DeviceCodeState, ApiRequestError, string>({
    mutationFn: (name) => api.post<DeviceCodeState>('/accounts/add/copilot', { name }),
    // The POST creates a NEW flow and returns its first state. Seed that state
    // before the component enables polling, otherwise a cached terminal result
    // from the previous attempt can stop the new poll immediately.
    onSuccess: (state, name) => { qc.setQueryData(QK.copilotAddStatus(name), state) },
  })
}

// Poll the device-flow state until authorization completes or fails.
// Mirrors useReauthStatus / useAddAccountStatus — polling stops automatically
// at terminal states (authorized | failed) to avoid unnecessary requests.
export function useCopilotAddStatus(name: string, enabled: boolean) {
  return useQuery<DeviceCodeState>({
    queryKey: QK.copilotAddStatus(name),
    queryFn: () => api.get<DeviceCodeState>(`/accounts/add/copilot?name=${encodeURIComponent(name)}`),
    enabled,
    refetchInterval: (query) => {
      const s = query.state.data?.state
      return s === 'authorized' || s === 'failed' ? false : 2000
    },
    refetchOnWindowFocus: false,
    staleTime: 0,
  })
}

export function useCancelCopilotAdd() {
  const qc = useQueryClient()
  return useMutation<DeviceCodeState, ApiRequestError, string>({
    mutationFn: (name) => api.post<DeviceCodeState>('/accounts/add/copilot/cancel', { name }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.accounts }) },
  })
}

// ── OpenAI / ChatGPT device flow ─────────────────────────────────────────────
//
// The SAME state machine as Copilot, on different routes — the backend spells
// the two payloads identically on purpose, so these are modelled line-for-line
// on the Copilot trio above rather than being a third dialect. Polling stops at
// the terminal states (authorized | failed); nothing here writes a userCode or
// verificationUri anywhere that outlives the query cache.

export function useAddOpenAIAccount() {
  const qc = useQueryClient()
  return useMutation<DeviceCodeState, ApiRequestError, string>({
    mutationFn: (name) => api.post<DeviceCodeState>('/accounts/add/openai', { name }),
    onSuccess: (state, name) => { qc.setQueryData(QK.openaiAddStatus(name), state) },
  })
}

export function useOpenAIAddStatus(name: string, enabled: boolean) {
  return useQuery<DeviceCodeState>({
    queryKey: QK.openaiAddStatus(name),
    queryFn: () => api.get<DeviceCodeState>(`/accounts/add/openai?name=${encodeURIComponent(name)}`),
    enabled,
    refetchInterval: (query) => {
      const s = query.state.data?.state
      return s === 'authorized' || s === 'failed' ? false : 2000
    },
    refetchOnWindowFocus: false,
    staleTime: 0,
  })
}

export function useCancelOpenAIAdd() {
  const qc = useQueryClient()
  return useMutation<DeviceCodeState, ApiRequestError, string>({
    mutationFn: (name) => api.post<DeviceCodeState>('/accounts/add/openai/cancel', { name }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.accounts }) },
  })
}

// An OpenAI account RE-authenticates by re-running its device flow at the
// credential authority, so `/accounts/{name}/reauth` answers with a device-code
// snapshot rather than a `ReauthState` (there is no browser OAuth and no local
// slot to re-encrypt). Same URLs as `useReauthAccount`/`useReauthStatus`,
// different payload — hence a DISTINCT query key: sharing ['reauth-status', name]
// would hand one reader the other's shape.

export function useOpenAIReauth() {
  const qc = useQueryClient()
  return useMutation<DeviceCodeState, ApiRequestError, string>({
    mutationFn: (name) => api.post<DeviceCodeState>(`/accounts/${encodeURIComponent(name)}/reauth`),
    onSuccess: (state, name) => { qc.setQueryData(QK.openaiReauthStatus(name), state) },
  })
}

export function useOpenAIReauthStatus(name: string, enabled: boolean) {
  return useQuery<DeviceCodeState>({
    queryKey: QK.openaiReauthStatus(name),
    queryFn: () => api.get<DeviceCodeState>(`/accounts/${encodeURIComponent(name)}/reauth`),
    enabled,
    refetchInterval: (query) => {
      const s = query.state.data?.state
      return s === 'authorized' || s === 'failed' ? false : 2000
    },
    refetchOnWindowFocus: false,
    staleTime: 0,
  })
}

export function useCancelOpenAIReauth() {
  const qc = useQueryClient()
  return useMutation<DeviceCodeState, ApiRequestError, string>({
    mutationFn: (name) =>
      api.post<DeviceCodeState>(`/accounts/${encodeURIComponent(name)}/reauth/cancel`),
    // fire-and-forget cancel: never toast on error (see queryClient global handler)
    meta: { suppressErrorToast: true },
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.accounts }) },
  })
}

export function useArchivedSessions() {
  return useQuery<ArchivedSession[]>({
    queryKey: QK.archived,
    queryFn: () => api.get<ArchivedSession[]>('/sessions/archived'),
    staleTime: 10_000,
    refetchInterval: 30_000,
  })
}

// Single source of truth for the unarchive endpoint — reused by useUnarchiveSession
// and the archive Undo action.
function unarchiveRequest(id: string) {
  return api.post<ArchivedSession>(`/sessions/${id}/unarchive`)
}

export function useUnarchiveSession() {
  const qc = useQueryClient()
  return useMutation<ArchivedSession, ApiRequestError, string, { prev?: ArchivedSession[]; removed?: ArchivedSession }>({
    mutationFn: (id) => unarchiveRequest(id),
    onMutate: async (id) => {
      await qc.cancelQueries({ queryKey: QK.archived })
      const prev = qc.getQueryData<ArchivedSession[]>(QK.archived)
      const removed = prev?.find((x) => x.id === id)
      qc.setQueryData<ArchivedSession[]>(QK.archived, (cur) => (cur ?? []).filter((x) => x.id !== id))
      return { prev, removed }
    },
    onError: (e, _id, ctx) => {
      if (ctx?.prev) qc.setQueryData(QK.archived, ctx.prev)
      const serverMsg = e instanceof ApiRequestError && e.status === 507
        ? (e.body.detail as { message?: string } | null)?.message
        : undefined
      toast.error(serverMsg ?? `Couldn't restore ${ctx?.removed?.name ?? 'session'}`)
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: QK.sessions })
      qc.invalidateQueries({ queryKey: QK.archived })
    },
  })
}

export function usePermanentDeleteSession() {
  const qc = useQueryClient()
  return useMutation<void, ApiRequestError, string, { prevArchived?: ArchivedSession[]; prevSessions?: Session[]; name?: string }>({
    mutationFn: (id) => api.del<void>(`/sessions/${id}/permanent`),
    onMutate: async (id) => {
      await qc.cancelQueries({ queryKey: QK.archived })
      await qc.cancelQueries({ queryKey: QK.sessions })
      const prevArchived = qc.getQueryData<ArchivedSession[]>(QK.archived)
      const prevSessions = qc.getQueryData<Session[]>(QK.sessions)
      const name = prevArchived?.find((x) => x.id === id)?.name ?? prevSessions?.find((x) => x.id === id)?.name
      qc.setQueryData<ArchivedSession[]>(QK.archived, (cur) => (cur ?? []).filter((x) => x.id !== id))
      qc.setQueryData<Session[]>(QK.sessions, (cur) => (cur ?? []).filter((x) => x.id !== id))
      return { prevArchived, prevSessions, name }
    },
    onError: (_e, _id, ctx) => {
      if (ctx?.prevArchived) qc.setQueryData(QK.archived, ctx.prevArchived)
      if (ctx?.prevSessions) qc.setQueryData(QK.sessions, ctx.prevSessions)
      toast.error(`Couldn't delete ${ctx?.name ?? 'session'}`)
    },
    // No success toast: the card leaves the list optimistically in onMutate,
    // which is the acknowledgement. Only the failure needs a voice.
    onSettled: () => {
      qc.invalidateQueries({ queryKey: QK.archived })
      qc.invalidateQueries({ queryKey: QK.sessions })
    },
  })
}

export function useBulkUnarchiveSessions() {
  const qc = useQueryClient()
  return useMutation<BatchResult, ApiRequestError, string[], { prev?: ArchivedSession[] }>({
    mutationFn: (ids) => api.post<BatchResult>('/sessions/batch/unarchive', { ids }),
    onMutate: async (ids) => {
      await qc.cancelQueries({ queryKey: QK.archived })
      const prev = qc.getQueryData<ArchivedSession[]>(QK.archived)
      const idSet = new Set(ids)
      qc.setQueryData<ArchivedSession[]>(QK.archived, (cur) => (cur ?? []).filter((x) => !idSet.has(x.id)))
      return { prev }
    },
    onError: (_e, _ids, ctx) => {
      if (ctx?.prev) qc.setQueryData(QK.archived, ctx.prev)
      toast.error(`Couldn't restore sessions`)
    },
    onSuccess: (result) => {
      const n = result.succeeded.length
      const f = result.failed.length
      if (f > 0) toast.error(`Restored ${n} · ${f} failed`)
      else toast.success(`Restored ${n}`)
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: QK.sessions })
      qc.invalidateQueries({ queryKey: QK.archived })
    },
  })
}

export function useBulkArchiveSessions() {
  const qc = useQueryClient()
  return useMutation<BatchResult, ApiRequestError, string[], { prev?: Session[] }>({
    mutationFn: (ids) => api.post<BatchResult>('/sessions/batch/archive', { ids }),
    onMutate: async (ids) => {
      await qc.cancelQueries({ queryKey: QK.sessions })
      const prev = qc.getQueryData<Session[]>(QK.sessions)
      const idSet = new Set(ids)
      qc.setQueryData<Session[]>(QK.sessions, (cur) => (cur ?? []).filter((x) => !idSet.has(x.id)))
      return { prev }
    },
    onError: (_e, _ids, ctx) => {
      if (ctx?.prev) qc.setQueryData(QK.sessions, ctx.prev)
      toast.error(`Couldn't archive sessions`)
    },
    onSuccess: (result) => {
      const n = result.succeeded.length
      const f = result.failed.length
      if (f > 0) toast.error(`Archived ${n} · ${f} failed`)
      else toast.success(`Archived ${n}`)
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: QK.sessions })
      qc.invalidateQueries({ queryKey: QK.archived })
    },
  })
}

export function useResumeInterrupted() {
  const qc = useQueryClient()
  return useMutation<BatchResult, ApiRequestError, string[]>({
    mutationFn: (ids) => api.post<BatchResult>('/sessions/batch/resume', { ids }),
    onSuccess: (result) => {
      const n = result.succeeded.length
      const f = result.failed.length
      if (f > 0) toast.error(`Resumed ${n} · ${f} skipped`)
      else toast.success(`Resumed ${n}`)
    },
    onSettled: () => { qc.invalidateQueries({ queryKey: QK.sessions }) },
  })
}

export function useRotateStalled() {
  const qc = useQueryClient()
  return useMutation<BatchResult, ApiRequestError, string[]>({
    mutationFn: (ids) => api.post<BatchResult>('/sessions/batch/rotate', { ids }),
    onSuccess: (result) => {
      const n = result.succeeded.length
      const f = result.failed.length
      if (f > 0) toast.error(`Rotated ${n} · ${f} skipped`)
      else toast.success(`Rotated ${n}`)
    },
    onSettled: () => { qc.invalidateQueries({ queryKey: QK.sessions }) },
  })
}

export interface BulkDeleteProgress {
  done: number
  total: number
}

const DELETE_CHUNK_SIZE = 10

/**
 * Permanently delete many archived sessions, chunked for live progress + cancel.
 *
 * The backend processes a batch sequentially and only replies when the whole
 * batch is done, so a single 95-id request is a multi-minute black box. We slice
 * the ids into chunks and fire one request per chunk, advancing `progress` after
 * each. Succeeded ids are pruned from the cache incrementally so the list shrinks
 * in step with the bar. Best-effort: a chunk whose request throws has its ids
 * counted as failed and the loop continues. `cancel()` stops the loop at the next
 * chunk boundary; already-deleted sessions stay deleted.
 */
export function useBulkPermanentDeleteSessions() {
  const qc = useQueryClient()
  const [progress, setProgress] = useState<BulkDeleteProgress | null>(null)
  const cancelRef = useRef(false)

  const mutation = useMutation<BatchResult & { aborted: boolean }, ApiRequestError, string[]>({
    mutationFn: async (ids) => {
      cancelRef.current = false
      const succeeded: string[] = []
      const failed: { id: string; error: string }[] = []
      setProgress({ done: 0, total: ids.length })
      for (let i = 0; i < ids.length; i += DELETE_CHUNK_SIZE) {
        if (cancelRef.current) break
        const chunk = ids.slice(i, i + DELETE_CHUNK_SIZE)
        try {
          const res = await api.post<BatchResult>('/sessions/batch/permanent-delete', { ids: chunk })
          succeeded.push(...res.succeeded)
          failed.push(...res.failed)
          const okSet = new Set(res.succeeded)
          qc.setQueryData<ArchivedSession[]>(QK.archived, (cur) => (cur ?? []).filter((x) => !okSet.has(x.id)))
        } catch {
          for (const id of chunk) failed.push({ id, error: 'request failed' })
        }
        setProgress({ done: Math.min(i + DELETE_CHUNK_SIZE, ids.length), total: ids.length })
      }
      return { succeeded, failed, aborted: cancelRef.current }
    },
    onSuccess: (result) => {
      const n = result.succeeded.length
      const f = result.failed.length
      const stopped = result.aborted ? ' · stopped' : ''
      if (f > 0) toast.error(`Deleted ${n} · ${f} failed${stopped}`)
      else toast.success(`Deleted ${n}${stopped}`)
    },
    onSettled: () => {
      setProgress(null)
      cancelRef.current = false
      qc.invalidateQueries({ queryKey: QK.archived })
      qc.invalidateQueries({ queryKey: QK.sessions })
    },
  })

  const cancel = useCallback(() => { cancelRef.current = true }, [])

  return { ...mutation, progress, cancel }
}

/** Live untracked claude sessions eligible for adoption. Polled while the panel is open. */
export function useAdoptable() {
  return useQuery<{ candidates: AdoptionCandidate[] }>({
    queryKey: QK.adoptable,
    queryFn: () => api.get<{ candidates: AdoptionCandidate[] }>('/sessions/adoptable'),
    staleTime: 3_000,
    refetchInterval: 5_000,
  })
}

/** Force-take-over an untracked session, then refresh both the sessions list and candidates. */
export function useAdoptSession() {
  const qc = useQueryClient()
  return useMutation<AdoptOutcome, ApiRequestError, string>({
    mutationFn: (claudeSessionId) =>
      api.post<AdoptOutcome>('/sessions/adopt', { claudeSessionId }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.sessions })
      qc.invalidateQueries({ queryKey: QK.adoptable })
    },
  })
}

export interface SessionTranscript {
  events: SessionEvent[]
  /** Lowest turn ordinal in this response (0 = session start). */
  earliestTurn?: number
  /** Total logical turns on disk — earliestTurn > 0 means older turns exist. */
  totalTurns?: number
}

/** With `limit`, fetches only the last `limit` turns (older ones page in via
 *  fetchOlderTurns); without it, the full transcript. */
export function useSessionTranscript(id: string, enabled: boolean, limit?: number) {
  return useQuery<SessionTranscript>({
    queryKey: ['session-transcript', id, limit ?? null],
    queryFn: () => api.get<SessionTranscript>(
      `/sessions/${id}/transcript${limit != null ? `?limit=${limit}` : ''}`),
    enabled,
    staleTime: Infinity,
  })
}

function useDeployStatusResponse() {
  return useQuery<DeployStatusResponse>({
    queryKey: ['deploy', 'status'],
    queryFn: fetchDeployStatus,
    refetchInterval: 5_000,
  })
}

export function usePendingDeploy() {
  const q = useDeployStatusResponse()
  return { ...q, data: q.data?.pending as PendingDeploy | undefined }
}

export function useDeployStatus() {
  const q = useDeployStatusResponse()
  return { ...q, data: q.data?.deploy as DeployProgress | undefined }
}

/**
 * Settings › Fleet's one PATCH surface (enabled / weight / max sessions / ssh
 * config), `PUT /api/fleet/hosts/{name}`. Lives here — not inline in
 * `FleetSection` — so a rejected write (e.g. a "max sessions" value the
 * server refuses) gets the same `onError` treatment every other mutation in
 * this file gets instead of silently vanishing: the hosts query still gets
 * invalidated (a 409 can mean the row moved under the operator) and the
 * failure reaches them as a toast, not nothing.
 */
export function useFleetHostPatch() {
  const qc = useQueryClient()
  return useMutation<FleetHost, ApiRequestError, { name: string; body: Partial<FleetHost> }>({
    mutationFn: ({ name, body }) =>
      api.put<FleetHost>(`/fleet/hosts/${encodeURIComponent(name)}`, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: FLEET_HOSTS_KEY })
      void qc.invalidateQueries({ queryKey: QK.capacity })
      // effectiveDefaults.globalCap rides on GET /api/settings too (the Hosts
      // footer and the Limits rows both read it) — without this a host cap
      // edit leaves "Σ caps = N → physical ceiling" showing the stale sum.
      void qc.invalidateQueries({ queryKey: QK.settings })
    },
    onError: (e) => {
      void qc.invalidateQueries({ queryKey: FLEET_HOSTS_KEY })
      toast.error(extractMutationErrorMessage(e, 'Could not update the host'))
    },
  })
}

export function useStartDeploy() {
  const qc = useQueryClient()
  // The variable is an optional explicit target sha (green-frontier deploy);
  // undefined deploys origin/master's tip as always.
  return useMutation<{ started: boolean; reason?: string }, ApiRequestError, string | undefined>({
    mutationFn: (targetSha) => startDeploy(targetSha),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['deploy', 'status'] }),
  })
}

export function useAllPrs() {
  return useQuery<PipelinePr[]>({
    queryKey: QK.prs,
    queryFn: () => api.get<PipelinePr[]>('/prs'),
    staleTime: 5_000,
    refetchInterval: 10_000,
  })
}

export function useStageModels({ enabled = true, poll = true }: { enabled?: boolean; poll?: boolean } = {}) {
  return useQuery<Record<string, StageModel>>({
    queryKey: QK.pipelineRepos,
    queryFn: () => api.get<Record<string, StageModel>>('/pipeline/repos'),
    staleTime: 15_000,
    refetchInterval: poll ? 30_000 : false,
    enabled,
  })
}

export function useAutomations({ poll = false }: { poll?: boolean } = {}) {
  return useQuery<Automation[]>({
    queryKey: QK.automations, queryFn: fetchAutomations, staleTime: 10_000,
    refetchInterval: poll ? 30_000 : false })
}

/** The capacity governor's readout (design §5). `poll` is owned by whichever
 *  caller is the live page (`CapacityDetail`); the list column's copy (via
 *  `useDeliveryWorkspace`) reads the same cache entry without a second timer —
 *  the same split as `useAutomations`/`useStageModels` above. */
export function useCapacity({ poll = false, enabled = true }: { poll?: boolean; enabled?: boolean } = {}) {
  return useQuery<CapacityReadout>({
    queryKey: QK.capacity,
    queryFn: fetchCapacity,
    staleTime: 10_000,
    refetchInterval: poll ? 30_000 : false,
    enabled,
  })
}

/** The nav dot's signal: the governor's persisted binding constraint (one-row
 *  read), polled app-wide at the governor's own 60 s tick. Nested under
 *  `QK.capacity` so every dial mutation's invalidation refreshes it too. */
export function useCapacityBinding() {
  return useQuery({
    queryKey: [...QK.capacity, 'binding'] as const,
    queryFn: fetchCapacityBinding,
    staleTime: 30_000,
    refetchInterval: 60_000,
  })
}

export function useAutomationEvents(id: string, enabled = true) {
  return useQuery<AutomationEvent[]>({
    queryKey: QK.automationEvents(id), queryFn: () => fetchAutomationEvents(id),
    enabled, staleTime: 10_000 })
}

export type SpawnedSession = { id: string; name: string; status: string; originRef: string | null }

export function useAutomationSessions(id: string, enabled = true) {
  return useQuery<SpawnedSession[]>({
    queryKey: QK.automationSessions(id), queryFn: () => fetchSpawnedSessions(id),
    enabled, staleTime: 10_000 })
}

export function usePatchAutomation() {
  const qc = useQueryClient()
  return useMutation<Automation, ApiRequestError, { id: string; body: AutomationPatch }>({
    mutationFn: ({ id, body }) => patchAutomation(id, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.automations })
      // The capacity governor is an automation (id `capacity-governor`); its
      // push level and mode are two of the readout's own fields (design
      // 2026-09-19 §5/§7.1), so a change here must not leave the Capacity
      // page showing a stale push level until the next 30s poll.
      qc.invalidateQueries({ queryKey: QK.capacity })
    },
  })
}

export function useCheckInsights(repo: string | undefined, window: string, { poll = true }: { poll?: boolean } = {}) {
  return useQuery<CheckInsights>({
    queryKey: QK.pipelineInsights(repo, window),
    queryFn: () =>
      api.get<CheckInsights>(
        `/pipeline/insights/checks?repo=${encodeURIComponent(repo as string)}&window=${window}`),
    staleTime: 15_000,
    refetchInterval: poll ? 30_000 : false,
    enabled: !!repo,
  })
}

export function useAutomationActionable(id: string, enabled = true) {
  return useQuery<ActionableItem[]>({
    queryKey: QK.automationActionable(id),
    queryFn: () => fetchAutomationActionable(id),
    enabled, staleTime: 10_000 })
}

export function useAutomationTrigger(id: string) {
  const qc = useQueryClient()
  return useMutation<TriggerResult, ApiRequestError, { item_id: string; kind: string }>({
    mutationFn: (body) => postAutomationTrigger(id, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.automationActionable(id) })
      qc.invalidateQueries({ queryKey: QK.automations })
    },
  })
}

/** The clustered inbox (#2936). Polls slowly: the model pass that names themes
 *  lands on the daemon's next tick after the inbox changes. */
export function useFeedbackClusters(id: string, enabled = true) {
  return useQuery<InboxClusters>({
    queryKey: QK.inboxClusters(id),
    queryFn: () => fetchInboxClusters(id),
    enabled, staleTime: 10_000, refetchInterval: 60_000 })
}

/** A generated issue for the reports `ids`. A POST, but a read: nothing is
 *  filed, so it is a query — fetched when the editor opens, never cached past it. */
export function useFeedbackClusterDraft(id: string, ids: string[], enabled = true) {
  return useQuery<ClusterDraft>({
    // Its own key, NOT under inboxClusters: an inbox write invalidates that
    // prefix, and re-drafting reports that just left the inbox would 422.
    queryKey: QK.inboxDraft(id, ids),
    queryFn: () => postClusterDraft(id, ids),
    enabled, staleTime: 0, gcTime: 0, retry: false })
}

/** Every inbox write moves rows out of the inbox: refresh the clusters, the flat
 *  actionable list (the tab badge) and the automations list (the nav badge). */
function useInboxWrite<V>(id: string, fn: (vars: V) => Promise<TriggerResult>) {
  const qc = useQueryClient()
  return useMutation<TriggerResult, ApiRequestError, V>({
    mutationFn: fn,
    onSettled: () => {
      qc.invalidateQueries({ queryKey: QK.inboxClusters(id) })
      qc.invalidateQueries({ queryKey: QK.automationActionable(id) })
      qc.invalidateQueries({ queryKey: QK.automations })
    },
  })
}

export function useFileFeedbackCluster(id: string) {
  return useInboxWrite<{ ids: string[]; title: string; body: string }>(id, (b) => postClusterFile(id, b))
}

export function useDismissFeedbackCluster(id: string) {
  return useInboxWrite<{ ids: string[]; fixedBy?: number }>(id, (b) => postClusterDismiss(id, b))
}

export function useAttachFeedbackCluster(id: string) {
  return useInboxWrite<{ ids: string[] } & ClusterAttachTarget>(id, (b) => postClusterAttach(id, b))
}

/** Aged-out reports back into the inbox (they rejoin the clusters and the badge). */
export function useRestoreFeedbackReports(id: string) {
  return useInboxWrite<{ ids: string[] }>(id, (b) => postClusterRestore(id, b))
}

export function useConsolidation(enabled = true) {
  return useQuery<ConsolidationView>({
    queryKey: QK.consolidation,
    queryFn: fetchConsolidation,
    enabled,
    staleTime: 10_000,
  })
}

/** The backlog grouping optimizer's current plan (read-only visibility). */
export function useBacklogGroups(enabled = true) {
  return useQuery<BacklogGroupsView>({
    queryKey: QK.backlogGroups,
    queryFn: fetchBacklogGroups,
    enabled,
    staleTime: 10_000,
  })
}

/** Current eligible issues for manual dispatch. Only fetches when enabled. */
export function useBacklogCandidates(enabled = true) {
  return useQuery<BacklogCandidatesView>({
    queryKey: QK.backlogCandidates,
    queryFn: fetchBacklogCandidates,
    enabled,
    staleTime: 30_000,
  })
}

/** Mutation: immediately claim + spawn a session for the given refs. */
export function useDispatchBacklogGroup() {
  const qc = useQueryClient()
  return useMutation<DispatchGroupResponse, ApiRequestError, string[]>({
    mutationFn: (refs) => postDispatchGroup(refs),
    // ManualDispatchDialog / BacklogGroupsPanel show the failure inline.
    meta: { suppressErrorToast: true },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.backlogGroups })
      qc.invalidateQueries({ queryKey: QK.automationSessions('backlog-runner') })
      qc.invalidateQueries({ queryKey: QK.backlogCandidates })
      // A dispatch RELEASES an owner_action hold on its refs server-side (spec
      // 2026-09-18 §7, signal "implement") and that happens before the spawn, so
      // it holds for a refusal too. Every surface that asserts the hold has to
      // refetch, or the roadmap keeps its chip and the dialog keeps offering to
      // release a hold that is already gone. `['pm', 'issue']` is the prefix of
      // `QK.workItemIssue(id)` — the dispatch does not know which id is open.
      qc.invalidateQueries({ queryKey: ['pm', 'issue'] })
      qc.invalidateQueries({ queryKey: ['pm', 'leaves'] })
      qc.invalidateQueries({ queryKey: QK.roadmapMap })
      qc.invalidateQueries({ queryKey: QK.queue })
    },
  })
}

export function useConsolidate() {
  const qc = useQueryClient()
  return useMutation<TriggerResult, ApiRequestError, { repo: string; numbers: number[] }>({
    mutationFn: (body) => postConsolidate(body),
    // ConsolidatePanel shows the outcome inline.
    meta: { suppressErrorToast: true },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.consolidation })
      qc.invalidateQueries({ queryKey: QK.automationActionable('pr-doctor') })
      qc.invalidateQueries({ queryKey: QK.automationSessions('pr-doctor') })
    },
  })
}

// --- Library (plugins / skills / commands / MCP) ---

export function useLibraryPlugins() {
  return useQuery<LibraryPluginsResponse>({
    queryKey: QK.libraryPlugins,
    queryFn: () => api.get<LibraryPluginsResponse>('/library/plugins'),
    staleTime: 30_000,
  })
}

export function useLibraryPluginDetail(id: string, enabled: boolean) {
  return useQuery<LibraryPluginDetail>({
    queryKey: QK.libraryPlugin(id),
    queryFn: () => api.get<LibraryPluginDetail>(`/library/plugins/${encodeURIComponent(id)}`),
    enabled,
    staleTime: 60_000,
  })
}

export function useTogglePlugin() {
  const qc = useQueryClient()
  return useMutation<{ id: string; enabled: boolean }, ApiRequestError, { id: string; enabled: boolean }>({
    mutationFn: ({ id, enabled }) =>
      api.patch(`/library/plugins/${encodeURIComponent(id)}`, { enabled }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: QK.libraryPlugins })
      void qc.invalidateQueries({ queryKey: ['library', 'plugin'] })
      // plugin enablement changes which skills/commands/servers are visible
      void qc.invalidateQueries({ queryKey: QK.librarySkills })
      void qc.invalidateQueries({ queryKey: QK.libraryCommands })
      void qc.invalidateQueries({ queryKey: QK.libraryMcp })
    },
  })
}

export function useLibrarySkills() {
  return useQuery<{ skills: LibrarySkill[] }>({
    queryKey: QK.librarySkills,
    queryFn: () => api.get('/library/skills'),
    staleTime: 30_000,
  })
}

export function useLibraryCommands() {
  return useQuery<{ commands: LibraryCommand[] }>({
    queryKey: QK.libraryCommands,
    queryFn: () => api.get('/library/commands'),
    staleTime: 30_000,
  })
}

export type LibraryItemKind = 'skills' | 'commands'

export function useLibraryItemContent(kind: LibraryItemKind, id: string, plugin?: string) {
  const suffix = plugin ? `?plugin=${encodeURIComponent(plugin)}` : ''
  return useQuery<LibraryContent>({
    queryKey: QK.libraryContent(kind, id, plugin),
    queryFn: () => api.get<LibraryContent>(`/library/${kind}/${id}${suffix}`),
    enabled: id.length > 0,
  })
}

export function useSaveLibraryItem(kind: LibraryItemKind) {
  const qc = useQueryClient()
  return useMutation<unknown, ApiRequestError, { id: string; content: string }>({
    mutationFn: ({ id, content }) => api.put(`/library/${kind}/${id}`, { content }),
    onSuccess: (_data, { id }) => {
      void qc.invalidateQueries({ queryKey: kind === 'skills' ? QK.librarySkills : QK.libraryCommands })
      void qc.invalidateQueries({ queryKey: QK.libraryContent(kind, id) })
    },
  })
}

export function useDeleteLibraryItem(kind: LibraryItemKind) {
  const qc = useQueryClient()
  return useMutation<unknown, ApiRequestError, { id: string }>({
    mutationFn: ({ id }) => api.del(`/library/${kind}/${id}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: kind === 'skills' ? QK.librarySkills : QK.libraryCommands })
    },
  })
}

export function useLibraryMcp() {
  return useQuery<{ servers: LibraryMcpServer[] }>({
    queryKey: QK.libraryMcp,
    queryFn: () => api.get('/library/mcp'),
    staleTime: 30_000,
  })
}

export function useSaveMcpServer() {
  const qc = useQueryClient()
  return useMutation<unknown, ApiRequestError, { name: string; config: McpServerConfig }>({
    mutationFn: ({ name, config }) =>
      api.put(`/library/mcp/${encodeURIComponent(name)}`, { config }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: QK.libraryMcp }),
  })
}

export function useDeleteMcpServer() {
  const qc = useQueryClient()
  return useMutation<unknown, ApiRequestError, { name: string }>({
    mutationFn: ({ name }) => api.del(`/library/mcp/${encodeURIComponent(name)}`),
    onSuccess: () => void qc.invalidateQueries({ queryKey: QK.libraryMcp }),
  })
}

// ─── Dispatch assess (#dispatch-auto-select) ──────────────────────────────

/** Plain async helper (not a query/mutation) — fire-and-forget from DispatchBar. */
export function useAssessDispatch(): { assess: (prompt: string) => Promise<AssessResponse | null> } {
  const assess = useCallback(async (prompt: string): Promise<AssessResponse | null> => {
    try {
      return await api.post<AssessResponse>('/dispatch/assess', { prompt })
    } catch {
      return null
    }
  }, [])
  return { assess }
}

// ─── Presets (#597) ───────────────────────────────────────────────────────

export function usePresets() {
  return useQuery<SessionPreset[]>({
    queryKey: ['presets'],
    queryFn: () => api.get<SessionPreset[]>('/presets'),
  })
}

export function useCreatePreset() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: Partial<SessionPreset> & { name: string }) =>
      api.post('/presets', body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['presets'] }),
  })
}

export function useUpdatePreset() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, ...body }: Partial<SessionPreset> & { id: string }) =>
      api.patch(`/presets/${id}`, body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['presets'] }),
  })
}

export function useDeletePreset() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.del(`/presets/${id}`),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['presets'] }),
  })
}

// ─── Templates (#598) ─────────────────────────────────────────────────────

export function useTemplates(tag?: string) {
  return useQuery<PromptTemplate[]>({
    queryKey: ['templates', tag],
    queryFn: () => {
      const url = tag ? `/templates?tag=${encodeURIComponent(tag)}` : '/templates'
      return api.get<PromptTemplate[]>(url)
    },
  })
}

export function useCreateTemplate() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: { name: string; content: string; variables?: unknown[]; tags?: string[] }) =>
      api.post('/templates', body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['templates'] }),
  })
}

export function useUpdateTemplate() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, ...body }: Partial<PromptTemplate> & { id: string }) =>
      api.patch(`/templates/${id}`, body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['templates'] }),
  })
}

export function useDeleteTemplate() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.del(`/templates/${id}`),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['templates'] }),
  })
}

// ─── Permission Profiles (#600) ───────────────────────────────────────────

export function usePermissionProfiles() {
  return useQuery<PermissionProfile[]>({
    queryKey: ['permission-profiles'],
    queryFn: () => api.get<PermissionProfile[]>('/permission-profiles'),
  })
}

export function useCreatePermissionProfile() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: { name: string; description?: string; mode?: string; patterns?: string[] }) =>
      api.post('/permission-profiles', body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['permission-profiles'] }),
  })
}

export function useUpdatePermissionProfile() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, ...body }: Partial<PermissionProfile> & { id: string }) =>
      api.patch(`/permission-profiles/${id}`, body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['permission-profiles'] }),
  })
}

export function useDeletePermissionProfile() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.del(`/permission-profiles/${id}`),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['permission-profiles'] }),
  })
}

// ─── Always-allow standing rules ──────────────────────────────────────────

export function usePermissionRules(sessionId?: string) {
  return useQuery<RuleWire[]>({
    queryKey: ['permission-rules', sessionId ?? 'all'],
    queryFn: () => api.get<RuleWire[]>(
      sessionId ? `/permission-rules?sessionId=${encodeURIComponent(sessionId)}` : '/permission-rules'),
  })
}

export function useUpdatePermissionRule() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, ...body }: { id: string; pattern: string; confirm?: boolean }) =>
      api.patch<RuleWire>(`/permission-rules/${id}`, body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['permission-rules'] }),
  })
}

export function useRevokePermissionRule() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.del(`/permission-rules/${id}`),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['permission-rules'] }),
  })
}

// ── SDLC initiatives (issues #802/#803) ──────────────────────────────────────

/** The PM / SDLC mutations below all opt out of the global error toast: each
 *  consumer renders `mutation.error` inline beside the control that fired it
 *  (via `extractMutationErrorMessage` from queryClient, which already unwraps
 *  the `{detail: {error, message}}` body shape these endpoints return). Before
 *  this every failure produced two toasts — the hook's own and the global one. */
const PM_MUTATION_META = { suppressErrorToast: true } as const

/** The human-gate tap. On success, refresh the map (every list of initiatives
 *  the roadmap renders comes from it) and this initiative's own detail. */
export function useAdvanceInitiative() {
  const qc = useQueryClient()
  return useMutation<Initiative, ApiRequestError, string>({
    mutationFn: (slug: string) => advanceInitiative(slug),
    onSuccess: (updated) => {
      void qc.invalidateQueries({ queryKey: QK.initiative(updated.slug) })
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
    },
    meta: PM_MUTATION_META,
  })
}

/** One stage back. Same invalidations as advance: the map lists it, the detail shows it. */
export function useStepBackInitiative() {
  const qc = useQueryClient()
  return useMutation<Initiative, ApiRequestError, string>({
    mutationFn: (slug) => stepBackInitiative(slug),
    onSuccess: (updated) => {
      void qc.invalidateQueries({ queryKey: QK.initiative(updated.slug) })
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
    },
    meta: PM_MUTATION_META,
  })
}

/** Clear a block. The row leaves the Needs-you pile's blocked section, so the map refreshes too. */
export function useUnblockInitiative() {
  const qc = useQueryClient()
  return useMutation<Initiative, ApiRequestError, string>({
    mutationFn: (slug) => unblockInitiative(slug),
    onSuccess: (updated) => {
      void qc.invalidateQueries({ queryKey: QK.initiative(updated.slug) })
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
    },
    meta: PM_MUTATION_META,
  })
}

/** The retry for a plan whose decomposition the synthesizer gave up on (#1582).
 *  Invalidates exactly what `useUnblockInitiative` does — it clears the same
 *  block, so the row leaves the Needs-you pile's blocked section the same way. */
export function useDecomposeAgain() {
  const qc = useQueryClient()
  return useMutation<Initiative, ApiRequestError, string>({
    mutationFn: (slug) => decomposeAgain(slug),
    onSuccess: (updated) => {
      void qc.invalidateQueries({ queryKey: QK.initiative(updated.slug) })
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
    },
    meta: PM_MUTATION_META,
  })
}

/** The initiative's latest decomposition proposal, or `null` when it has never
 *  been decomposed (#992). Polled on the detail's own cadence: the proposal
 *  appears when a decompose session's ingest lands, which is not a tap this page
 *  made, so the card has to arrive without a reload. */
export function useMaterialization(slug: string | null) {
  return useQuery<MaterializationProposal | null>({
    queryKey: QK.materialization(slug ?? ''),
    queryFn: () => fetchMaterialization(slug as string),
    enabled: slug !== null && slug !== '',
    staleTime: 5_000,
    refetchInterval: 15_000,
  })
}

/** The human gate on a decomposition: create the epic, its sub-issues and the
 *  chain. A failure is NOT a rollback — the proposal stays pending and the apply
 *  resumes from its journal — so the card renders `error` in place and keeps
 *  offering the same tap (`PM_MUTATION_META` suppresses the global toast).
 *
 *  One invalidation covers the proposal AND the detail: `QK.materialization`
 *  extends `QK.initiative`, and the map is refreshed too because the initiative
 *  just gained the epic its roadmap row reports on. */
export function useAcceptMaterialization() {
  const qc = useQueryClient()
  return useMutation<MaterializationApplied, ApiRequestError, string>({
    mutationFn: (slug) => acceptMaterialization(slug),
    onSuccess: (_result, slug) => {
      void qc.invalidateQueries({ queryKey: QK.initiative(slug) })
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
    },
    meta: PM_MUTATION_META,
  })
}

/** Turn a decomposition down. No map invalidation: a rejection changes nothing
 *  the roadmap shows — the initiative stays parked exactly where it was. */
export function useRejectMaterialization() {
  const qc = useQueryClient()
  return useMutation<MaterializationProposal, ApiRequestError, string>({
    mutationFn: (slug) => rejectMaterialization(slug),
    onSuccess: (_result, slug) => void qc.invalidateQueries({ queryKey: QK.initiative(slug) }),
    meta: PM_MUTATION_META,
  })
}

export function useCreateInitiative() {
  const qc = useQueryClient()
  return useMutation<Initiative, ApiRequestError, CreateInitiativeInput>({
    mutationFn: (input) => createInitiative(input),
    onSuccess: () => void qc.invalidateQueries({ queryKey: QK.roadmapMap }),
    meta: PM_MUTATION_META,
  })
}

/** Inline edit of an initiative's title / capabilities. The detail and the map
 *  both carry the capability links (the map places the ◆ row under its primary),
 *  so both are invalidated. */
export function useUpdateInitiative() {
  const qc = useQueryClient()
  return useMutation<Initiative, ApiRequestError, { slug: string; input: UpdateInitiativeInput }>({
    mutationFn: ({ slug, input }) => updateInitiative(slug, input),
    onSuccess: (_result, { slug }) => {
      void qc.invalidateQueries({ queryKey: QK.initiative(slug) })
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
    },
    onError: (e) => toast.error(extractMutationErrorMessage(e, 'Could not update the initiative')),
    meta: PM_MUTATION_META,
  })
}

/** Adopt an already-committed doc as a stage artifact (#2160). The detail carries
 *  the artifact chain and the next-action; the map's stage track summarizes it. */
export function useAdoptArtifact() {
  const qc = useQueryClient()
  return useMutation<InitiativeArtifact, ApiRequestError, { slug: string; input: AdoptArtifactInput }>({
    mutationFn: ({ slug, input }) => adoptArtifact(slug, input),
    onSuccess: (_result, { slug }) => {
      void qc.invalidateQueries({ queryKey: QK.initiative(slug) })
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
    },
    meta: PM_MUTATION_META,
  })
}

/** The control-band incident queue (#804). Polled on the initiatives cadence —
 *  the monitor's own tick is far slower (5 min) than either, and the board
 *  invalidates this alongside the tracks on `/ws` status frames. */
export function useIncidents() {
  return useQuery<Incident[]>({
    queryKey: QK.incidents,
    queryFn: fetchIncidents,
    staleTime: 5_000,
    refetchInterval: 30_000,
  })
}

/** Disposition one incident: triage now / schedule / dismiss.
 *
 *  NOT optimistic, unlike the PM triage inbox's accept/reject. Those remove a row
 *  outright, so a rollback is invisible; here a disposition CHANGES a row that
 *  stays on screen, and every failure mode is a server-side refusal with a reason
 *  worth reading (a past `scheduledFor`, an incident Intake already adopted).
 *  Showing the row's real state after the server answers is the honest rendering.
 *
 *  The map is invalidated too: `triage_now` is the tap that eventually produces
 *  an Initiative, and dropping that would leave the roadmap stale until its own
 *  poll. */
export function useIncidentAction() {
  const qc = useQueryClient()
  return useMutation<Incident, ApiRequestError, IncidentActionInput>({
    mutationFn: (input) => actOnIncident(input),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: QK.incidents })
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
    },
    meta: PM_MUTATION_META,
  })
}

// ── PM product layer (phase 1) ────────────────────────────────────────────────

/**
 * Registering a product adds a row to the roadmap map — the ONE query the
 * roadmap's list column and portfolio read products from (there is no separate
 * `/pm/projects` list observer any more, so invalidating that key would refresh
 * nothing anybody watches).
 */
export function useCreateProject() {
  const qc = useQueryClient()
  return useMutation<Project, ApiRequestError, CreateProjectInput>({
    mutationFn: (input) => createProject(input),
    onSuccess: () => void qc.invalidateQueries({ queryKey: QK.roadmapMap }),
    meta: PM_MUTATION_META,
  })
}

export function useSyncProject() {
  const qc = useQueryClient()
  return useMutation<{ requested: boolean }, ApiRequestError, string>({
    mutationFn: (slug) => requestProjectSync(slug),
    onSuccess: (_d, slug) => {
      // The endpoint is a 202 — the loop picks the request up on its next
      // tick — so without this the button read as "nothing happened".
      toast.success(`Sync requested for ${slug}`)
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
      void qc.invalidateQueries({ queryKey: QK.project(slug) })
    },
    meta: PM_MUTATION_META,
  })
}

// ── PM product layer (phase 2) — onboarding + triage hooks ───────────────────

/** Onboarding-wizard state for one project (a view over the engine). */
export function useOnboarding(slug: string) {
  return useQuery<OnboardingView>({
    queryKey: QK.onboarding(slug),
    queryFn: () => fetchOnboarding(slug),
    enabled: !!slug,
    staleTime: 5_000,
    refetchInterval: 15_000,
  })
}

// ── Roadmap (spec 2026-09-07 §10) ────────────────────────────────────────────

/**
 * The whole product → capability → initiative map with server-computed tiers.
 *
 * `enabled` exists because one caller is mounted app-wide: `AppShell` builds the
 * Roadmap list column from this on every route, and the column only RENDERS on
 * the Roadmap section (and never in kiosk, which has no list region at all). Left
 * ungated, a 15 s poll of `/pm/map` would run on all seven sections and on every
 * wall display, funding a column that is not mounted. The query key is unchanged
 * either way, so `RoadmapPage`'s own `useRoadmapMap()` still shares this cache
 * entry rather than opening a second fetch.
 *
 * `poll` is the other half of that: react-query runs the interval of EVERY
 * observer of a key, so the shell's column and the page beside it were each
 * ticking `/pm/map` — one shared entry, two requests every 15 s. The shell
 * observer (always mounted while the section is open) keeps the interval; the
 * pages pass `poll: false` and read the entry it refreshes.
 */
export function useRoadmapMap(
  { enabled = true, poll = true }: { enabled?: boolean; poll?: boolean } = {},
) {
  return useQuery<RoadmapMap>({
    queryKey: QK.roadmapMap,
    queryFn: fetchRoadmapMap,
    enabled,
    staleTime: 5_000,
    refetchInterval: poll ? 15_000 : false,
  })
}

/** Issues under one capability or initiative; `null` scope = not mounted yet. */
export function useLeaves(scope: LeavesScope | null) {
  const key = scope === null ? 'none'
    : scope.capability !== undefined ? `capability:${scope.capability}` : `initiative:${scope.initiative}`
  return useQuery<Leaf[]>({
    queryKey: QK.leaves(key),
    queryFn: () => fetchLeaves(scope as LeavesScope),
    enabled: scope !== null,
    staleTime: 5_000,
    refetchInterval: 30_000,
  })
}

/** Mounted only while reviewing an issue; reading never performs a disposition. */
export function useWorkItemIssue(itemId: string) {
  return useQuery<WorkItemIssue>({
    queryKey: QK.workItemIssue(itemId),
    queryFn: () => fetchWorkItemIssue(itemId),
    refetchOnWindowFocus: false,
  })
}

export function useDispositionIssue(itemId: string) {
  const qc = useQueryClient()
  return useMutation<IssueDispositionResult, ApiRequestError, IssueDispositionInput>({
    mutationFn: (input) => dispositionIssue(itemId, input),
    onSuccess: (result) => {
      qc.setQueryData(QK.workItemIssue(itemId), result.item)
      void qc.invalidateQueries({ queryKey: ['pm', 'leaves'] })
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
      void qc.invalidateQueries({ queryKey: QK.queue })
      void qc.invalidateQueries({ queryKey: QK.triage })
      void qc.invalidateQueries({ queryKey: ['sdlc', 'initiative'] })
    },
    meta: PM_MUTATION_META,
  })
}

/** Every view that asserts a hold, invalidated by prefix: the issue dialog, the
 *  leaf lists, the map's tiers/counts, the queue — and the escalation read model
 *  (`QK.holds`), which the Roadmap's held list and the Inbox's starvation banner
 *  read; without it a released hold kept its card for up to the 30 s poll. */
function invalidateHoldViews(qc: ReturnType<typeof useQueryClient>, itemId: string) {
  void qc.invalidateQueries({ queryKey: QK.workItemIssue(itemId) })
  void qc.invalidateQueries({ queryKey: ['pm', 'leaves'] })
  void qc.invalidateQueries({ queryKey: QK.roadmapMap })
  void qc.invalidateQueries({ queryKey: QK.queue })
  void qc.invalidateQueries({ queryKey: QK.holds })
}

export function useReleaseHold(itemId: string) {
  const qc = useQueryClient()
  return useMutation<HoldReleaseResult, ApiRequestError, void>({
    mutationFn: () => releaseHold(itemId),
    onSuccess: () => invalidateHoldViews(qc, itemId),
    meta: PM_MUTATION_META,
  })
}

/** Approve the climb an approval-gated hand-off asked for: releases the hold and
 *  dispatches the issue now at `model`, approving the gate's parked session as
 *  this person (server `POST …/hold/approve-escalation`). Failures show inline,
 *  like the other hold taps, so the toast layer stays quiet. */
export function useApproveEscalation(itemId: string) {
  const qc = useQueryClient()
  return useMutation<ApproveEscalationResult, ApiRequestError, string>({
    mutationFn: (model) => approveEscalation(itemId, model),
    onSuccess: () => {
      invalidateHoldViews(qc, itemId)
      void qc.invalidateQueries({ queryKey: QK.automationSessions('backlog-runner') })
    },
    meta: PM_MUTATION_META,
  })
}

/** Every Backlog Runner dispatch a work item has had, newest first — the
 *  history behind the roadmap's "bounced N× — needs spec" chip. `enabled`
 *  defaults true so a detail panel mounted already-open fetches immediately;
 *  a lazily-opened one (e.g. a disclosure) passes `enabled: false` until opened. */
export function useOutcomes(workItemId: string, { enabled = true }: { enabled?: boolean } = {}) {
  return useQuery<DispatchOutcome[]>({
    queryKey: QK.outcomes(workItemId),
    queryFn: () => fetchOutcomes(workItemId),
    enabled,
    staleTime: 5_000,
  })
}

/** Remove a blocked-by edge from a leaf. Every leaves list is invalidated by
 *  prefix — the row does not know which scope(s) it is rendered under — plus the
 *  map, whose blocked counts and initiative tiers read the same dep rows, and
 *  every initiative detail by prefix: the input carries only a `workItemId`, so
 *  the row cannot say which initiative's epic-blocked count it just changed. */
export function useRemoveDependency() {
  const qc = useQueryClient()
  return useMutation<RemoveDependencyResult, ApiRequestError, RemoveDependencyInput>({
    mutationFn: (input) => removeDependency(input),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['pm', 'leaves'] })
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
      void qc.invalidateQueries({ queryKey: ['sdlc', 'initiative'] })
    },
    meta: PM_MUTATION_META,
  })
}

/** The ranked cross-product queue — the same query the runner will read (spec 003 C1). */
export function useQueue() {
  return useQuery<QueueView>({
    queryKey: QK.queue,
    queryFn: fetchQueue,
    staleTime: 5_000,
    refetchInterval: 30_000,
  })
}

/** Held items and per-product dispatch health (escalation read model, spec 2026-09-30 §4). */
export function useHolds(
  product?: string,
  { enabled = true }: { enabled?: boolean } = {},
) {
  return useQuery<HoldsView>({
    queryKey: [...QK.holds, product ?? null],
    queryFn: () => fetchHolds(product),
    enabled,
    staleTime: 5_000,
    refetchInterval: 30_000,
  })
}

/** One initiative with children, sessions and next action. */
export function useInitiative(slug: string | null) {
  return useQuery<Initiative>({
    queryKey: QK.initiative(slug ?? ''),
    queryFn: () => fetchInitiative(slug as string),
    enabled: slug !== null && slug !== '',
    staleTime: 5_000,
    refetchInterval: 15_000,
  })
}

/** The gate registry with per-tolerance auto-resolution (`GET /sdlc/gates`).
 *  `staleTime: Infinity`: it changes only with a deploy. */
export function useSdlcGates() {
  return useQuery<GateRegistry>({
    queryKey: QK.gates,
    queryFn: fetchGates,
    staleTime: Infinity,
  })
}

/** One project: repos and edges (the list payload's counts are not here).
 *  `poll` defaults true (every existing caller's behaviour); a reader that
 *  mounts many of these at once (e.g. one per row of the Capacity products
 *  table, `CapacityProductRow`) passes `poll: false` so N rows don't open N
 *  15s timers over the same handful of `QK.project` keys — a `staleTime`-only
 *  refetch on remount/focus is enough for a value only shown, not live-edited,
 *  by that reader. */
export function useProject(slug: string | null, { poll = true }: { poll?: boolean } = {}) {
  return useQuery<Project>({
    queryKey: QK.project(slug ?? ''),
    queryFn: () => fetchProject(slug as string),
    enabled: slug !== null && slug !== '',
    staleTime: 5_000,
    refetchInterval: poll ? 15_000 : false,
  })
}

/** The ONE automation-policy schema every product's policy validates against
 *  (server `PRODUCT_POLICY_SCHEMA`, spec §4). `staleTime: Infinity`: it only
 *  changes on a deploy, never per-request — no poll, no re-fetch on focus. */
export function usePolicySchema() {
  return useQuery<ConfigField[]>({
    queryKey: QK.policySchema,
    queryFn: fetchPolicySchema,
    staleTime: Infinity,
  })
}

/** Repository registration evidence; never a cached permit for an action. */
export function useProductPolicyCoverage(slug: string) {
  return useQuery({
    queryKey: QK.policyCoverage(slug),
    queryFn: () => fetchProductPolicyCoverage(slug),
    enabled: slug !== '',
    staleTime: 30_000,
  })
}

/** Read-only implementation eligibility; parent project invalidation refreshes it after edits. */
export function useProductImplementationPolicy(slug: string) {
  return useQuery({
    queryKey: QK.implementationPolicy(slug),
    queryFn: () => fetchProductImplementationPolicy(slug),
    enabled: slug !== '',
    staleTime: 30_000,
  })
}

/** Sparse edits retain unrelated choices; conflicts refresh without retrying. */
export function useUpdateProductPolicy(slug: string) {
  const qc = useQueryClient()
  return useMutation<Project, ApiRequestError, ProductPolicyPatch>({
    mutationFn: (patch) => updateProductPolicy(slug, patch),
    retry: false,
    onSuccess: async (project) => {
      qc.setQueryData(QK.project(slug), project)
      await Promise.all([
        qc.invalidateQueries({ queryKey: QK.project(slug) }),
        qc.invalidateQueries({ queryKey: QK.roadmapMap }),
        // `capacity_mode`/`max_in_flight` are two of the readout's own product
        // fields (design 2026-09-19 §5) — a policy edit must not leave the
        // Capacity page showing the pre-edit value until its next poll.
        qc.invalidateQueries({ queryKey: QK.capacity }),
      ])
    },
    onError: async (e) => {
      if (e.status === 409) await qc.invalidateQueries({ queryKey: QK.project(slug) })
      toast.error(extractMutationErrorMessage(e, 'Could not update the automation policy'))
    },
    meta: PM_MUTATION_META,
  })
}

/**
 * Renames a product's name/slug and/or repoints its primary repo. A slug
 * change moves the cache entry to the new key — the caller navigates to the
 * new `productPath` on success, so the old key's observer is gone by the
 * time this settles; dropping it here avoids a stale 404 refetch either way.
 */
export function useUpdateProject(slug: string) {
  const qc = useQueryClient()
  return useMutation<Project, ApiRequestError, UpdateProjectInput>({
    mutationFn: (input) => updateProject(slug, input),
    retry: false,
    onSuccess: async (project) => {
      const newSlug = project.slug ?? slug
      if (newSlug !== slug) qc.removeQueries({ queryKey: QK.project(slug) })
      qc.setQueryData(QK.project(newSlug), project)
      await Promise.all([
        qc.invalidateQueries({ queryKey: QK.project(newSlug) }),
        qc.invalidateQueries({ queryKey: QK.roadmapMap }),
        // `priorityRank` is one of the readout's own product fields — see
        // `useReorderProject` below, this hook's sibling for the Capacity
        // products table's rank buttons.
        qc.invalidateQueries({ queryKey: QK.capacity }),
      ])
    },
    onError: async (e) => {
      if (e.status === 409) await qc.invalidateQueries({ queryKey: QK.project(slug) })
      toast.error(extractMutationErrorMessage(e, 'Could not update the product'))
    },
    meta: PM_MUTATION_META,
  })
}

/**
 * Re-ranks one product by slug (design 2026-09-19 §7.1's rank ▲▼ buttons).
 * Deliberately NOT `useUpdateProject(slug)`: that hook bakes its slug into the
 * mutation's closure at hook-creation time, but a rank swap always touches
 * TWO products (the moved row and its neighbour — see
 * `features/delivery/capacity/swapRanks.ts`) whose slugs are only known at
 * click time. This hook takes `slug` as a mutation VARIABLE instead, so one
 * instance (created once, not per-row) can `mutateAsync` for either side in
 * sequence without a second, differently-slugged hook instance.
 */
export function useReorderProject() {
  const qc = useQueryClient()
  return useMutation<Project, ApiRequestError, { slug: string; priorityRank: number }>({
    mutationFn: ({ slug, priorityRank }) => updateProject(slug, { priorityRank }),
    retry: false,
    onSuccess: async (project, { slug }) => {
      qc.setQueryData(QK.project(slug), project)
      await Promise.all([
        qc.invalidateQueries({ queryKey: QK.project(slug) }),
        qc.invalidateQueries({ queryKey: QK.roadmapMap }),
        qc.invalidateQueries({ queryKey: QK.capacity }),
      ])
    },
    onError: async (e, { slug }) => {
      if (e.status === 409) await qc.invalidateQueries({ queryKey: QK.project(slug) })
    },
    meta: PM_MUTATION_META,
  })
}

/** The product overview's hygiene notice (`RoadmapHealthCard`): unparented
 *  capabilities, unlinked leaf initiatives, stale-proposed capabilities and the
 *  stale-inbox count. Stale enough at 30s — this is a notice, not a live feed. */
export function useProjectHealth(slug: string) {
  return useQuery<ProjectHealth>({
    queryKey: QK.health(slug),
    queryFn: () => fetchProjectHealth(slug),
    staleTime: 30_000,
  })
}

/** Product dependency chips (spec §8 "Product dependency"). The project payload
 *  carries the edges; the map's product tiers read them; the deps panel below
 *  the chips polls its own key. */
export function useCreateEdge(slug: string) {
  const qc = useQueryClient()
  return useMutation<ProjectEdgeInfo, ApiRequestError, CreateEdgeInput>({
    mutationFn: (input) => createEdge(slug, input),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: QK.project(slug) })
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
      void qc.invalidateQueries({ queryKey: ['pm', 'deps', slug] })
    },
    onError: (e) => toast.error(extractMutationErrorMessage(e, 'Could not add the dependency')),
    meta: PM_MUTATION_META,
  })
}

/** Optimistic, in the triage hooks' shape: removing a chip is a direct,
 *  non-modal affordance with no loading state of its own, so the chip has to go
 *  the instant it is clicked and come back with a toast if the request fails. */
export function useDeleteEdge(slug: string) {
  const qc = useQueryClient()
  const key = QK.project(slug)
  return useMutation<void, ApiRequestError, string, { prev?: Project }>({
    mutationFn: (edgeId) => deleteEdge(slug, edgeId),
    onMutate: async (edgeId) => {
      await qc.cancelQueries({ queryKey: key })
      const prev = qc.getQueryData<Project>(key)
      qc.setQueryData<Project>(key, (cur) => (
        cur ? { ...cur, edges: cur.edges.filter((e) => e.id !== edgeId) } : cur))
      return { prev }
    },
    onError: (e, _edgeId, ctx) => {
      if (ctx?.prev) qc.setQueryData(key, ctx.prev)
      toast.error(extractMutationErrorMessage(e, 'Could not remove the dependency'))
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
      void qc.invalidateQueries({ queryKey: ['pm', 'deps', slug] })
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: key }),
    meta: PM_MUTATION_META,
  })
}

/**
 * R8c-5: the two GitHub-flavored failures get their own copy — a 409 `conflict`
 * means another edit landed first (the form stays open with the user's values so
 * they can just retry), a 502 `github` names which step GitHub refused. Anything
 * else (422 validation, `not_behind`, `no_open_map_changes`) surfaces the
 * server's own `message`, falling back when the body carries none.
 */
export function mapChangesErrorMessage(e: unknown, fallback: string): string {
  if (!(e instanceof ApiRequestError)) return fallback
  const detail = e.body?.detail
  const record = detail && typeof detail === 'object' ? (detail as Record<string, unknown>) : null
  const error = record && typeof record.error === 'string' ? record.error : null
  const step = record && typeof record.step === 'string' ? record.step : null
  const message = record && typeof record.message === 'string' ? record.message : null
  if (e.status === 409 && error === 'conflict') return 'The map changed under you — try again.'
  if (e.status === 502 && error === 'github') return `GitHub refused the ${step ?? 'request'} step: ${message ?? fallback}`
  return message ?? fallback
}

/** One capability edit (R8c-1/R8c-2's dialogs send one op per request, in
 *  sequence). Invalidates both the map (tree, PR badges) and the product (the
 *  Overview's PR card and capability list).
 *
 *  No toast on failure: all three callers (the two dialogs and the links editor)
 *  render the mapped message inline in a `role="alert"` right next to the form
 *  that failed, which is what `PM_MUTATION_META` ("the caller renders it") is
 *  for — a toast on top of that says the same thing twice. Abandon and Rebase
 *  DO toast: they are fired from a banner with nowhere to put the message. */
export function useEditMapChanges(slug: string) {
  const qc = useQueryClient()
  return useMutation<MapChangeResult, ApiRequestError, MapChangeInput>({
    mutationFn: (input) => editMapChanges(slug, input),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
      void qc.invalidateQueries({ queryKey: QK.project(slug) })
    },
    meta: PM_MUTATION_META,
  })
}

export function useRetireCapability(slug: string) {
  const qc = useQueryClient()
  return useMutation<RetireCapabilityResult, ApiRequestError, RetireCapabilityInput>({
    mutationFn: (input) => retireCapability(slug, input),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
      void qc.invalidateQueries({ queryKey: QK.project(slug) })
    },
    meta: PM_MUTATION_META,
  })
}

export function useMergeCapabilities(slug: string) {
  const qc = useQueryClient()
  return useMutation<MergeCapabilityResult, ApiRequestError, MergeCapabilitiesInput>({
    mutationFn: (input) => mergeCapabilities(slug, input),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
      void qc.invalidateQueries({ queryKey: QK.project(slug) })
    },
    meta: PM_MUTATION_META,
  })
}

/** R8c-4's one destructive roadmap action, gated behind a typed-slug confirm. */
export function useAbandonMapChanges(slug: string) {
  const qc = useQueryClient()
  return useMutation<AbandonResult, ApiRequestError, void>({
    mutationFn: () => abandonMapChanges(slug),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
      void qc.invalidateQueries({ queryKey: QK.project(slug) })
    },
    onError: (e) => toast.error(mapChangesErrorMessage(e, 'Could not abandon the map changes')),
    meta: PM_MUTATION_META,
  })
}

/** The PR card's Rebase tap (R8c-3) — only offered while the pointer reports `behind`. */
export function useRebaseMapChanges(slug: string) {
  const qc = useQueryClient()
  return useMutation<{ pointer: MapChangesPointer }, ApiRequestError, void>({
    mutationFn: () => rebaseMapChanges(slug),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
      void qc.invalidateQueries({ queryKey: QK.project(slug) })
    },
    onError: (e) => toast.error(mapChangesErrorMessage(e, 'Could not rebase the PR')),
    meta: PM_MUTATION_META,
  })
}

export function useProposeMap() {
  const qc = useQueryClient()
  return useMutation<{ sessionId: string | null }, ApiRequestError, string>({
    mutationFn: (slug) => proposeMap(slug),
    onSuccess: (_d, slug) => {
      void qc.invalidateQueries({ queryKey: QK.onboarding(slug) })
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
    },
    meta: PM_MUTATION_META,
  })
}

/** issue #2259: spawn the constitution-proposal session. */
export function useProposeConstitution() {
  const qc = useQueryClient()
  return useMutation<{ sessionId: string | null }, ApiRequestError, string>({
    mutationFn: (slug) => proposeConstitution(slug),
    onSuccess: (_d, slug) => {
      void qc.invalidateQueries({ queryKey: QK.onboarding(slug) })
    },
    meta: PM_MUTATION_META,
  })
}

export function useClassify() {
  const qc = useQueryClient()
  return useMutation<{ dispatched: number }, ApiRequestError, string>({
    mutationFn: (slug) => runClassify(slug),
    onSuccess: (_d, slug) => {
      void qc.invalidateQueries({ queryKey: QK.onboarding(slug) })
      void qc.invalidateQueries({ queryKey: QK.triage })
      // Classification is what fills the awaiting-triage count the map carries.
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
    },
    meta: PM_MUTATION_META,
  })
}

/** The wizard's "map merged" tap (map_proposed → map_merged). */
export function useAdvanceMap() {
  const qc = useQueryClient()
  return useMutation<{ onboardingState: string }, ApiRequestError, string>({
    mutationFn: (slug) => advanceMap(slug),
    onSuccess: (_d, slug) => {
      void qc.invalidateQueries({ queryKey: QK.onboarding(slug) })
      // The onboarding state is on the product payload the detail pane reads,
      // and merging the map is what puts capabilities on the tree.
      void qc.invalidateQueries({ queryKey: QK.project(slug) })
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
    },
    meta: PM_MUTATION_META,
  })
}

/** The wizard's "go live" tap (map_merged|classifying → live). */
export function useAdvanceLive() {
  const qc = useQueryClient()
  return useMutation<{ onboardingState: string }, ApiRequestError, string>({
    mutationFn: (slug) => advanceLive(slug),
    onSuccess: (_d, slug) => {
      void qc.invalidateQueries({ queryKey: QK.onboarding(slug) })
      void qc.invalidateQueries({ queryKey: QK.project(slug) })
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
    },
    meta: PM_MUTATION_META,
  })
}

/**
 * The pending triage queue, optionally scoped to one project.
 *
 * `enabled` is NOT what keeps this off the other sections, unlike
 * `useRoadmapMap`'s: the Roadmap nav badge (`useNavBadges`) polls the unscoped
 * queue app-wide by design, so `/pm/triage` is live on Sessions, Pipeline,
 * Automations, Insights and Settings whatever the column does. The gate is what
 * stops the column — mounted on every route through `useRoadmapWorkspace`,
 * hooks always are — from being a SECOND observer of that key, and what keeps
 * the queue off a wall display, which renders no nav badges.
 *
 * `poll` for the reason `useRoadmapMap` has one: two polling observers of one
 * key are two intervals. Whoever already owns the interval for a key keeps it —
 * the badge for the unscoped queue, the list column for a `?product=` scope —
 * and the other observer reads what it fetched.
 */
export function useTriage(
  projectSlug?: string,
  { enabled = true, poll = true }: { enabled?: boolean; poll?: boolean } = {},
) {
  return useQuery<TriageProposal[]>({
    queryKey: [...QK.triage, projectSlug ?? null],
    queryFn: () => fetchTriage(projectSlug),
    enabled,
    staleTime: 5_000,
    refetchInterval: poll ? 15_000 : false,
  })
}

/**
 * Pending DECISION proposals (spec 2026-09-18 §6), on the same 15 s cadence as
 * `useTriage` and nudged by the same `/ws` hook.
 *
 * Scoped server-side like `useTriage` (spec 2026-09-30 §4.3): the key carries the
 * product and the `impact` flag, so the nav badge's bare `useDecisions()` (no
 * impact — it polls every 15 s and impact costs a queue read) and the Roadmap
 * column's impact-enriched list are different entries. `poll` and `enabled` follow
 * `useTriage`: whoever already owns a key's interval keeps it; `enabled` keeps the
 * column off kiosk, which renders no nav badges.
 */
export function useDecisions(
  projectSlug?: string,
  { enabled = true, poll = true, impact = false }: { enabled?: boolean; poll?: boolean; impact?: boolean } = {},
) {
  return useQuery<DecisionProposal[]>({
    queryKey: [...QK.decisions, projectSlug ?? null, impact],
    queryFn: () => fetchDecisions(projectSlug, { impact }),
    enabled,
    staleTime: 5_000,
    refetchInterval: poll ? 15_000 : false,
  })
}

/**
 * Live nudge for the triage inbox — the `useInitiativesLive` idiom keyed to
 * triage: classification runs in spawned sessions, so the queue grows on `/ws`
 * status frames. Baseline polling in `useTriage` covers the gaps.
 *
 * It nudges BOTH inbox lists from the one subscription. A decision proposal is
 * written by a coding session too (its `propose_decision` tool, then the session
 * ends), so the same status frame that grows the classification queue is what
 * announces a new decision — and a second hook would be a second `/ws`
 * subscription for one list.
 */
export function useTriageLive() {
  const qc = useQueryClient()
  const { on, off } = useSessionSocket()
  useEffect(() => {
    const nudge = () => {
      void qc.invalidateQueries({ queryKey: QK.triage })
      // `cancelRefetch: false`: the impact-enriched fetch is a queue read, and every
      // status frame would otherwise cancel and restart the one already in flight.
      void qc.invalidateQueries({ queryKey: QK.decisions }, { cancelRefetch: false })
    }
    on('status', nudge)
    return () => off('status', nudge)
  }, [on, off, qc])
}

/**
 * The triage mutations below are optimistic: the row leaves the list on `onMutate`
 * and comes back on `onError`. That removal unmounts the row, so there is no
 * inline error slot left to render into — these hooks own their failure message
 * as a toast. They KEEP `PM_MUTATION_META` for exactly the reason it exists: it
 * silences the global MutationCache toast so the failure is said once, here,
 * rather than twice.
 */

/** Six seconds to change your mind — same shape as the archive toast in
 *  `useCloseSession`. `ids` are the landed proposal ids; every one is undone. */
function triageUndoToast(qc: QueryClient, message: string, ids: string[], projectSlug?: string) {
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: QK.triage })
    void qc.invalidateQueries({ queryKey: QK.roadmapMap })
    if (projectSlug) void qc.invalidateQueries({ queryKey: QK.onboarding(projectSlug) })
  }
  toast.success(message, {
    duration: 6000,
    action: {
      label: 'Undo',
      onClick: () => {
        void Promise.allSettled(ids.map((id) => undoProposal(id))).then((results) => {
          const failed = results.filter((r) => r.status === 'rejected')
          if (failed.length) toast.error(failed.length === ids.length ? 'Could not undo' : `Could not undo ${failed.length} of ${ids.length}`)
          refresh()
        })
      },
    },
  })
}

/** Optimistic single-accept — remove the row now, roll back (and toast) on error. */
export function useAcceptProposal(projectSlug?: string) {
  const qc = useQueryClient()
  const key = [...QK.triage, projectSlug ?? null]
  return useMutation<
    { id: string; status: string },
    ApiRequestError,
    { id: string; edits?: OverlayEdits },
    { prev?: TriageProposal[] }
  >({
    mutationFn: ({ id, edits }) => acceptProposal(id, edits),
    onMutate: async ({ id }) => {
      await qc.cancelQueries({ queryKey: key })
      const prev = qc.getQueryData<TriageProposal[]>(key)
      qc.setQueryData<TriageProposal[]>(key, (cur) => (cur ?? []).filter((p) => p.id !== id))
      return { prev }
    },
    onError: (e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(key, ctx.prev)
      toast.error(extractMutationErrorMessage(e, 'Could not accept proposal'))
    },
    // Accepting a proposal homes a work item on a capability: the map's
    // awaiting-triage and issue counts both move, and the onboarding wizard's
    // pending-proposal count with them.
    onSuccess: (_res, vars) => {
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
      if (projectSlug) void qc.invalidateQueries({ queryKey: QK.onboarding(projectSlug) })
      triageUndoToast(qc, 'Accepted', [vars.id], projectSlug)
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: QK.triage }),
    meta: PM_MUTATION_META,
  })
}

/**
 * The optimistic single "resolve it away" mutation shared by reject and dismiss:
 * the row leaves the list now, comes back (with a toast) on failure, and a
 * landed one offers the undo toast. Both move the map's awaiting-triage /
 * needs-home counts. One body so the two cannot drift; accept keeps its own
 * because it also moves the onboarding wizard's count and carries edits.
 */
function useResolveProposal(
  projectSlug: string | undefined,
  run: (id: string) => Promise<{ id: string; status: string }>,
  { failure, success }: { failure: string; success: string },
) {
  const qc = useQueryClient()
  const key = [...QK.triage, projectSlug ?? null]
  return useMutation<
    { id: string; status: string },
    ApiRequestError,
    { id: string },
    { prev?: TriageProposal[] }
  >({
    mutationFn: ({ id }) => run(id),
    onMutate: async ({ id }) => {
      await qc.cancelQueries({ queryKey: key })
      const prev = qc.getQueryData<TriageProposal[]>(key)
      qc.setQueryData<TriageProposal[]>(key, (cur) => (cur ?? []).filter((p) => p.id !== id))
      return { prev }
    },
    onError: (e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(key, ctx.prev)
      toast.error(extractMutationErrorMessage(e, failure))
    },
    onSuccess: (_res, vars) => {
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
      triageUndoToast(qc, success, [vars.id], projectSlug)
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: QK.triage }),
    meta: PM_MUTATION_META,
  })
}

/** Optimistic single-reject: parks the item under an owner-action hold. */
export function useRejectProposal(projectSlug?: string) {
  return useResolveProposal(projectSlug, rejectProposal,
    { failure: 'Could not reject proposal', success: 'Rejected' })
}

/** Optimistic single-dismiss: "not roadmap work" — resolved with NO hold. A
 *  server without the route answers 404/405, which lands as the failure toast
 *  and the row comes back. */
export function useDismissProposal(projectSlug?: string) {
  return useResolveProposal(projectSlug, (id) => dismissProposal(id),
    { failure: 'Could not dismiss proposal', success: 'Dismissed' })
}

/** Accept-selected (the inbox's `A` key). Optimistically drops all selected rows. */
export function useAcceptBulk(projectSlug?: string) {
  const qc = useQueryClient()
  const key = [...QK.triage, projectSlug ?? null]
  return useMutation<
    AcceptBulkResult,
    ApiRequestError,
    string[],
    { prev?: TriageProposal[] }
  >({
    mutationFn: (ids) => acceptBulk(ids),
    onMutate: async (ids) => {
      await qc.cancelQueries({ queryKey: key })
      const prev = qc.getQueryData<TriageProposal[]>(key)
      const drop = new Set(ids)
      qc.setQueryData<TriageProposal[]>(key, (cur) => (cur ?? []).filter((p) => !drop.has(p.id)))
      return { prev }
    },
    onError: (e, _ids, ctx) => {
      if (ctx?.prev) qc.setQueryData(key, ctx.prev)
      toast.error(extractMutationErrorMessage(e, 'Could not accept selection'))
    },
    onSuccess: (res) => {
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
      if (projectSlug) void qc.invalidateQueries({ queryKey: QK.onboarding(projectSlug) })
      if (res.accepted) {
        triageUndoToast(qc, `Accepted ${res.accepted}`, res.results.filter((r) => r.ok).map((r) => r.id), projectSlug)
      }
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: QK.triage }),
    meta: PM_MUTATION_META,
  })
}

/**
 * The bulk twin of `useResolveProposal`, shared by reject- and dismiss-selected:
 * optimistically drops every selected row, restores them all on failure, and
 * offers one undo toast over the ids that landed.
 */
function useResolveBulk<R extends { results: BulkSweepRow[] }>(
  projectSlug: string | undefined,
  run: (ids: string[]) => Promise<R>,
  landed: (res: R) => number,
  { failure, success }: { failure: string; success: (n: number) => string },
) {
  const qc = useQueryClient()
  const key = [...QK.triage, projectSlug ?? null]
  return useMutation<R, ApiRequestError, string[], { prev?: TriageProposal[] }>({
    // Wrapped: react-query passes a second (context) argument the fetcher must not see.
    mutationFn: (ids) => run(ids),
    onMutate: async (ids) => {
      await qc.cancelQueries({ queryKey: key })
      const prev = qc.getQueryData<TriageProposal[]>(key)
      const drop = new Set(ids)
      qc.setQueryData<TriageProposal[]>(key, (cur) => (cur ?? []).filter((p) => !drop.has(p.id)))
      return { prev }
    },
    onError: (e, _ids, ctx) => {
      if (ctx?.prev) qc.setQueryData(key, ctx.prev)
      toast.error(extractMutationErrorMessage(e, failure))
    },
    onSuccess: (res) => {
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
      const n = landed(res)
      if (n) triageUndoToast(qc, success(n), res.results.filter((r) => r.ok).map((r) => r.id), projectSlug)
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: QK.triage }),
    meta: PM_MUTATION_META,
  })
}

/** Reject-selected (the inbox's `R` key) — the reject twin of `useAcceptBulk`:
 *  parks each item under an owner-action hold. */
export function useRejectBulk(projectSlug?: string) {
  return useResolveBulk(projectSlug, rejectBulk, (r) => r.rejected,
    { failure: 'Could not reject selection', success: (n) => `Rejected ${n}` })
}

/** Dismiss-selected (the inbox's `D` key): "not roadmap work", no holds. */
export function useDismissBulk(projectSlug?: string) {
  return useResolveBulk(projectSlug, dismissBulk, (r) => r.dismissed,
    { failure: 'Could not dismiss selection', success: (n) => `Dismissed ${n}` })
}

/** Inline overlay edit (edit-then-accept `e`, and manual score edits). */
export function useUpdateOverlay() {
  const qc = useQueryClient()
  return useMutation<
    WorkItemOverlay,
    ApiRequestError,
    { itemId: string; edit: OverlayEdits }
  >({
    mutationFn: ({ itemId, edit }) => updateOverlay(itemId, edit),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: QK.triage })
      // A score reorders every leaves list the row is in; a capability change
      // moves the row between nodes and shifts two nodes' counts on the map.
      void qc.invalidateQueries({ queryKey: ['pm', 'leaves'] })
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
    },
    onError: (e) => toast.error(extractMutationErrorMessage(e, 'Could not update work item')),
    meta: PM_MUTATION_META,
  })
}

/** Standalone undo — same endpoint the toasts' Undo action calls, exposed for a
 *  direct affordance (e.g. a "Recently decided" list) outside the toast window. */
export function useUndoProposal(projectSlug?: string) {
  const qc = useQueryClient()
  return useMutation<UndoResult, ApiRequestError, string>({
    mutationFn: (id) => undoProposal(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: QK.triage })
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
      if (projectSlug) void qc.invalidateQueries({ queryKey: QK.onboarding(projectSlug) })
    },
    onError: (e) => toast.error(extractMutationErrorMessage(e, 'Could not undo')),
    meta: PM_MUTATION_META,
  })
}

/**
 * Accept / reject one DECISION proposal (spec 2026-09-18 §6).
 *
 * Same optimistic shape as the classification twins above — the row leaves the
 * list on `onMutate`, comes back on `onError`, and the hook owns its failure
 * message because the removal has already unmounted the inline slot.
 *
 * With ONE deliberate difference: no undo toast. A triage accept has a ~60 s
 * server-side undo window; a decision has none (spec decision 3 — the machine
 * drafts, the person decides, and once taken it is taken), so offering an Undo
 * that no endpoint backs would be a lie. Reversing a decision means parking the
 * item again, which is what the accept guidance's "Reverse:" line says.
 *
 * Both invalidate the same three views because both move the item's hold: the
 * board/roadmap tier (`roadmapMap`), the dispatch queue, and the disposition
 * dialog's issue (`['pm','issue']` — a prefix, since only lazily-mounted dialogs
 * live under it and the proposal carries no id for the one that matters).
 */
function invalidateAfterDecision(qc: QueryClient) {
  void qc.invalidateQueries({ queryKey: QK.decisions })
  // A leaf row renders the hold chip and the needs-you tier from `Leaf.holdKind`,
  // so the list a person may be looking at while they decide has to be told too —
  // the same key the other two hold-moving mutations invalidate (`useReleaseHold`,
  // `useDispositionIssue`). Without it the leaf keeps saying "Awaiting your
  // decision" until `useLeaves`' own 30 s poll fires.
  void qc.invalidateQueries({ queryKey: ['pm', 'leaves'] })
  void qc.invalidateQueries({ queryKey: QK.roadmapMap })
  void qc.invalidateQueries({ queryKey: QK.queue })
  void qc.invalidateQueries({ queryKey: QK.holds })
  void qc.invalidateQueries({ queryKey: ['pm', 'issue'] })
}

function useDecisionMutation<V extends { id: string }>(
  run: (vars: V) => Promise<DecisionResult>,
  { failure, success }: { failure: string; success: string },
) {
  const qc = useQueryClient()
  return useMutation<DecisionResult, ApiRequestError, V>({
    mutationFn: run,
    onMutate: async ({ id }) => {
      await qc.cancelQueries({ queryKey: QK.decisions })
      // Prefix match: the list is cached per product and impact flag, and the row
      // must leave every one of them.
      qc.setQueriesData<DecisionProposal[]>({ queryKey: QK.decisions }, (rows) => rows?.filter((r) => r.id !== id))
    },
    onError: (e) => {
      // No snapshot to restore: the list is cached under N keys, so rollback is
      // the refetch `onSettled` already issues.
      toast.error(extractMutationErrorMessage(e, failure))
    },
    onSuccess: () => {
      invalidateAfterDecision(qc)
      toast.success(success)
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: QK.decisions }),
    meta: PM_MUTATION_META,
  })
}

/** Take the decision at the option the person picked. */
export function useAcceptDecision() {
  return useDecisionMutation<{ id: string; chosenKey: string }>(
    ({ id, chosenKey }) => acceptDecision(id, chosenKey),
    { failure: 'Could not record this decision', success: 'Decision recorded' },
  )
}

/** Refuse the options; the item becomes the person's own hold. */
export function useRejectDecision() {
  return useDecisionMutation<{ id: string }>(
    ({ id }) => rejectDecision(id),
    { failure: 'Could not reject this proposal', success: 'Proposal rejected' },
  )
}

// MCP OAuth auth flow — mirrors the reauth hooks pattern.
// useStartMcpAuth: initiates OAuth for a named MCP server.
export function useStartMcpAuth() {
  return useMutation<McpAuthState, ApiRequestError, { name: string; account?: string }>({
    mutationFn: ({ name, account }) =>
      api.post<McpAuthState>(`/library/mcp/${encodeURIComponent(name)}/auth`, account ? { account } : undefined),
  })
}

// useTestMcp: probes MCP servers now (all accounts, or one account/server) and refreshes the status rows.
export function useTestMcp() {
  const qc = useQueryClient()
  return useMutation<
    { rows: McpStatusRow[]; skipped: { account: string; reason: string }[] },
    ApiRequestError,
    { account?: string; server?: string }
  >({
    mutationFn: (body) => api.post('/library/mcp/test', body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: QK.libraryMcp }),
  })
}

// useSessionMcp: live MCP server states of one session (404 when the session is not live).
export function useSessionMcp(sessionId: string, enabled: boolean) {
  return useQuery<{ servers: McpServerState[] }>({
    queryKey: ['session-mcp', sessionId],
    queryFn: () => api.get(`/sessions/${encodeURIComponent(sessionId)}/mcp`),
    enabled,
    staleTime: 15_000,
    retry: false,
  })
}

// useReconnectSessionMcp: reconnects one MCP server inside a live session.
export function useReconnectSessionMcp(sessionId: string) {
  const qc = useQueryClient()
  return useMutation<{ servers: McpServerState[] }, ApiRequestError, { server: string }>({
    mutationFn: ({ server }) =>
      api.post(`/sessions/${encodeURIComponent(sessionId)}/mcp/${encodeURIComponent(server)}/reconnect`),
    onSuccess: (data) => qc.setQueryData(['session-mcp', sessionId], data),
  })
}

// useMcpAuthStatus: polls the auth state; stops automatically at terminal states.
export function useMcpAuthStatus(name: string, enabled: boolean) {
  return useQuery<McpAuthState>({
    queryKey: ['mcp-auth-status', name],
    queryFn: () => api.get<McpAuthState>(`/library/mcp/${encodeURIComponent(name)}/auth`),
    enabled,
    // Poll while auth is in flight; stop on terminal states to avoid unnecessary requests.
    refetchInterval: (query) => {
      const s = query.state.data?.state
      return s === 'success' || s === 'error' || s === 'cancelled' ? false : 1500
    },
    refetchOnWindowFocus: false,
    staleTime: 0,
  })
}

// useSubmitMcpRedirect: submits the OAuth redirect URL once the user completes the flow.
// Invalidates libraryMcp so the needs-auth badge refreshes.
export function useSubmitMcpRedirect() {
  const qc = useQueryClient()
  return useMutation<McpAuthState, ApiRequestError, { name: string; redirectUrl: string }>({
    mutationFn: ({ name, redirectUrl }) =>
      api.post<McpAuthState>(`/library/mcp/${encodeURIComponent(name)}/auth/redirect`, { redirectUrl }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: QK.libraryMcp }),
  })
}

// useCancelMcpAuth: cancels an in-flight MCP auth flow.
export function useCancelMcpAuth() {
  return useMutation<McpAuthState, ApiRequestError, string>({
    mutationFn: (name) => api.post<McpAuthState>(`/library/mcp/${encodeURIComponent(name)}/auth/cancel`),
    // fire-and-forget cancel: never toast on error (see queryClient global handler)
    meta: { suppressErrorToast: true },
  })
}
