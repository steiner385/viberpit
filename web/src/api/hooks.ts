import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState, useRef, useCallback, useSyncExternalStore } from 'react'
import { api, ApiRequestError } from './client'
import {
  fetchAutomations, fetchAutomationEvents, fetchSpawnedSessions, patchAutomation,
  fetchAutomationActionable, postAutomationTrigger,
  fetchConsolidation, postConsolidate, fetchBacklogGroups,
  fetchBacklogCandidates, postDispatchGroup,
  type Automation, type AutomationEvent, type AutomationPatch,
  type ActionableItem, type TriggerResult, type ConsolidationView,
  type BacklogGroupsView, type BacklogCandidatesView, type DispatchGroupResponse,
} from './automations'
import type {
  Account, Session, ReauthState, ArchivedSession, AdoptionCandidate, AdoptOutcome, ModelOption, BatchResult,
  DeployStatusResponse, PendingDeploy, DeployProgress, PipelinePr, StageModel, CheckInsights,
  DaemonHealth, CopilotAddState, McpAuthState,
  LibraryPluginDetail, LibraryPluginsResponse, LibrarySkill, LibraryCommand,
  LibraryMcpServer, McpServerConfig, LibraryContent,
  SessionPreset, PromptTemplate, PermissionProfile, AssessResponse,
} from './types'
import { fetchDeployStatus, startDeploy } from './deploy'
import {
  fetchInitiatives, advanceInitiative, createInitiative,
  type Initiative, type CreateInitiativeInput,
} from './sdlc'
import {
  fetchProjects, createProject, requestProjectSync,
  fetchOnboarding, proposeMap, runClassify,
  type Project, type CreateProjectInput, type OnboardingView,
} from './pm'
import {
  fetchTriage, acceptProposal, rejectProposal,
  type TriageProposal, type OverlayEdits,
} from './triage'
import type { SessionEvent } from '@/session/events'
import type { ToolDetailMode } from '@/session/groupBlocks'
import { getPendingSessions, subscribePending, addPendingSession, removePendingSession } from '@/lib/pendingSessions'
import { isSessionsViewing, setSessionsViewing } from './sessionsViewing'
import { reconcileSessions } from '@/session/reconcile'
import { toast } from 'sonner'

// Query keys
export const QK = {
  accounts: ['accounts'] as const,
  models: ['models'] as const,
  sessions: ['sessions'] as const,
  archived: ['archived-sessions'] as const,
  adoptable: ['adoptable-sessions'] as const,
  config: ['config'] as const,
  githubRepos: ['github-repos'] as const,
  githubBranches: (repo?: string) => ['github-branches', repo] as const,
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
  consolidation: ['automation-consolidation'] as const,
  backlogGroups: ['backlog-groups'] as const,
  backlogCandidates: ['backlog-candidates'] as const,
  daemonHealth: ['daemon-health'] as const,
  libraryPlugins: ['library', 'plugins'] as const,
  libraryPlugin: (id: string) => ['library', 'plugin', id] as const,
  librarySkills: ['library', 'skills'] as const,
  libraryCommands: ['library', 'commands'] as const,
  libraryMcp: ['library', 'mcp'] as const,
  libraryContent: (kind: string, id: string, plugin?: string) =>
    ['library', 'content', kind, id, plugin ?? null] as const,
  initiatives: ['sdlc', 'initiatives'] as const,
  initiative: (slug: string) => ['sdlc', 'initiative', slug] as const,
  projects: ['pm', 'projects'] as const,
  triage: ['pm', 'triage'] as const,          // phase 2 OWNS this key
  onboarding: (slug: string) => ['pm', 'onboarding', slug] as const,
}

export function useModels() {
  return useQuery<ModelOption[]>({
    queryKey: QK.models,
    queryFn: () => api.get<ModelOption[]>('/models'),
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

export interface GithubBranchInfo {
  default: string | null
  branches: string[]
}

export interface ConfigInfo {
  repoRoot: string
  adoptionEnabled: boolean
}

export function useConfig() {
  return useQuery<ConfigInfo>({
    queryKey: QK.config,
    queryFn: () => api.get<ConfigInfo>('/config'),
    staleTime: 60_000,
  })
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

export function useGithubBranches(repo?: string) {
  return useQuery<GithubBranchInfo>({
    queryKey: QK.githubBranches(repo),
    queryFn: () => api.get<GithubBranchInfo>(`/github/branches?repo=${encodeURIComponent(repo!)}`),
    enabled: !!repo,
    staleTime: 10_000,
  })
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
  // Maximum concurrent active sessions; sessions beyond this are queued. null = server default (5).
  maxActiveSessions: number | null
  // App-wide toggle: when false, completed sessions are not auto-archived (they linger
  // like a per-session manual opt-out). The server's NULL default surfaces here as true.
  autoArchiveEnabled: boolean
  // Global model-approval policy: 'require_approval' | 'block' | 'allow' | null (server default).
  modelApprovalAction: string | null
  // Comma-separated model ids/tiers that require approval; null = server default (fable).
  modelApprovalDenylist: string | null
}

export function useSettings() {
  return useQuery<AppSettings>({
    queryKey: QK.settings,
    queryFn: () => api.get<AppSettings>('/settings'),
    staleTime: 30_000,
  })
}

export function useUpdateSettings() {
  const qc = useQueryClient()
  return useMutation<AppSettings, ApiRequestError, Partial<AppSettings>>({
    mutationFn: (body) => api.put<AppSettings>('/settings', body),
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.settings }) },
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
  /** Optional runner preference. null → auto-rank; a name → that host first. */
  host?: string | null
}

export function useCreateSession() {
  const qc = useQueryClient()
  return useMutation<Session, ApiRequestError, CreateSessionInput, { tempId: string }>({
    mutationFn: (input) => api.post<Session>('/sessions', input),
    // Both callers (NewSessionDialogHost, PromptDock → DispatchBar) render the
    // failure inline next to the form that raised it; the global toast would be
    // a second, vaguer copy of the same message.
    meta: { suppressErrorToast: true },
    onMutate: (input) => {
      // Placeholder lives outside the query cache so the 5s poll can't wipe it
      // while the backend spins up the claude PTY.
      const tempId = addPendingSession({
        name: input.name?.trim() || input.initialPrompt?.trim().slice(0, 60) || 'New session',
        accountName: input.account ?? '',
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

export function useSendPrompt() {
  const qc = useQueryClient()
  return useMutation<Session, ApiRequestError, { id: string; text: string }>({
    mutationFn: ({ id, text }) => api.post<Session>(`/sessions/${id}/prompt`, { text }),
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

export function useModelApproval() {
  const qc = useQueryClient()
  return useMutation<Session, ApiRequestError, { id: string; decision: 'approve' | 'deny'; model?: string }>({
    mutationFn: ({ id, decision, model }) =>
      api.post<Session>(`/sessions/${id}/model-approval`, { decision, model }),
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
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.accounts }) },
  })
}

export function useSetAccountEnabled() {
  const qc = useQueryClient()
  return useMutation<Account, ApiRequestError, { name: string; enabled: boolean }>({
    mutationFn: ({ name, enabled }) =>
      api.patch<Account>(`/accounts/${encodeURIComponent(name)}/enabled`, { enabled }),
    // Refetch accounts so the dashboard + pickers reflect the new state at once.
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.accounts }) },
  })
}

type ReauthAccountInput = string | { name: string; mode?: 'login' | 'setup_token' }

export function useReauthAccount() {
  return useMutation<ReauthState, ApiRequestError, ReauthAccountInput>({
    mutationFn: (input) => {
      const { name, mode } = typeof input === 'string' ? { name: input, mode: undefined } : input
      const path = `/accounts/${encodeURIComponent(name)}/reauth${mode === 'setup_token' ? '?mode=setup_token' : ''}`
      return api.post<ReauthState>(path)
    },
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
  return useMutation<CopilotAddState, ApiRequestError, string>({
    mutationFn: (name) => api.post<CopilotAddState>('/accounts/add/copilot', { name }),
  })
}

// Poll the device-flow state until authorization completes or fails.
// Mirrors useReauthStatus / useAddAccountStatus — polling stops automatically
// at terminal states (authorized | failed) to avoid unnecessary requests.
export function useCopilotAddStatus(name: string, enabled: boolean) {
  return useQuery<CopilotAddState>({
    queryKey: ['copilot-add-status', name],
    queryFn: () => api.get<CopilotAddState>(`/accounts/add/copilot?name=${encodeURIComponent(name)}`),
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
  return useMutation<CopilotAddState, ApiRequestError, string>({
    mutationFn: (name) => api.post<CopilotAddState>('/accounts/add/copilot/cancel', { name }),
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

export function useStageModels() {
  return useQuery<Record<string, StageModel>>({
    queryKey: QK.pipelineRepos,
    queryFn: () => api.get<Record<string, StageModel>>('/pipeline/repos'),
    staleTime: 15_000,
    refetchInterval: 30_000,
  })
}

export function useAutomations() {
  return useQuery<Automation[]>({
    queryKey: QK.automations, queryFn: fetchAutomations, staleTime: 10_000 })
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
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.automations }) },
  })
}

export function useCheckInsights(repo: string | undefined, window: string) {
  return useQuery<CheckInsights>({
    queryKey: QK.pipelineInsights(repo, window),
    queryFn: () =>
      api.get<CheckInsights>(
        `/pipeline/insights/checks?repo=${encodeURIComponent(repo as string)}&window=${window}`),
    staleTime: 15_000,
    refetchInterval: 30_000,
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
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.automationActionable(id) }) },
  })
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

// ── SDLC initiatives (issues #802/#803) ──────────────────────────────────────

/** The PM / SDLC mutations below all opt out of the global error toast: each
 *  consumer renders `mutation.error` inline beside the control that fired it
 *  (via `extractMutationErrorMessage` from queryClient, which already unwraps
 *  the `{detail: {error, message}}` body shape these endpoints return). Before
 *  this every failure produced two toasts — the hook's own and the global one. */
const PM_MUTATION_META = { suppressErrorToast: true } as const

/** The Initiative board's list. Each row already carries its artifact chain, so
 *  the board renders N tracks from one call. Polled for baseline liveness; the
 *  board also nudges an invalidation on `/ws` status frames (useInitiativesLive). */
export function useInitiatives() {
  return useQuery<Initiative[]>({
    queryKey: QK.initiatives,
    queryFn: fetchInitiatives,
    staleTime: 5_000,
    refetchInterval: 15_000,
  })
}

/** The human-gate tap. On success, refresh the list so the moved node re-renders. */
export function useAdvanceInitiative() {
  const qc = useQueryClient()
  return useMutation<Initiative, ApiRequestError, string>({
    mutationFn: (slug: string) => advanceInitiative(slug),
    onSuccess: (updated) => {
      void qc.invalidateQueries({ queryKey: QK.initiatives })
      void qc.invalidateQueries({ queryKey: QK.initiative(updated.slug) })
    },
    meta: PM_MUTATION_META,
  })
}

export function useCreateInitiative() {
  const qc = useQueryClient()
  return useMutation<Initiative, ApiRequestError, CreateInitiativeInput>({
    mutationFn: (input) => createInitiative(input),
    onSuccess: () => void qc.invalidateQueries({ queryKey: QK.initiatives }),
    meta: PM_MUTATION_META,
  })
}

// ── PM product layer (phase 1) ────────────────────────────────────────────────

/** Projects registry list. Polled like initiatives (baseline liveness). */
export function useProjects() {
  return useQuery<Project[]>({
    queryKey: QK.projects,
    queryFn: fetchProjects,
    staleTime: 5_000,
    refetchInterval: 15_000,
  })
}

export function useCreateProject() {
  const qc = useQueryClient()
  return useMutation<Project, ApiRequestError, CreateProjectInput>({
    mutationFn: (input) => createProject(input),
    onSuccess: () => void qc.invalidateQueries({ queryKey: QK.projects }),
    meta: PM_MUTATION_META,
  })
}

export function useSyncProject() {
  const qc = useQueryClient()
  return useMutation<{ requested: boolean }, ApiRequestError, string>({
    mutationFn: (slug) => requestProjectSync(slug),
    onSuccess: () => void qc.invalidateQueries({ queryKey: QK.projects }),
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

export function useProposeMap() {
  const qc = useQueryClient()
  return useMutation<{ sessionId: string | null }, ApiRequestError, string>({
    mutationFn: (slug) => proposeMap(slug),
    onSuccess: (_d, slug) => {
      void qc.invalidateQueries({ queryKey: QK.onboarding(slug) })
      void qc.invalidateQueries({ queryKey: QK.projects })
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
    },
    meta: PM_MUTATION_META,
  })
}

export function useTriage(projectSlug?: string) {
  return useQuery<TriageProposal[]>({
    queryKey: [...QK.triage, projectSlug ?? null],
    queryFn: () => fetchTriage(projectSlug),
    staleTime: 5_000,
    refetchInterval: 15_000,
  })
}

export function useAcceptProposal(projectSlug?: string) {
  const qc = useQueryClient()
  return useMutation<
    { id: string; status: string },
    ApiRequestError,
    { id: string; edits?: OverlayEdits }
  >({
    mutationFn: ({ id, edits }) => acceptProposal(id, edits),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: [...QK.triage, projectSlug ?? null] })
      void qc.invalidateQueries({ queryKey: QK.projects })
    },
    meta: PM_MUTATION_META,
  })
}

export function useRejectProposal(projectSlug?: string) {
  const qc = useQueryClient()
  return useMutation<
    { id: string; status: string },
    ApiRequestError,
    { id: string }
  >({
    mutationFn: ({ id }) => rejectProposal(id),
    onSuccess: () =>
      void qc.invalidateQueries({ queryKey: [...QK.triage, projectSlug ?? null] }),
    meta: PM_MUTATION_META,
  })
}

// MCP OAuth auth flow — mirrors the reauth hooks pattern.
// useStartMcpAuth: initiates OAuth for a named MCP server.
export function useStartMcpAuth() {
  return useMutation<McpAuthState, ApiRequestError, string>({
    mutationFn: (name) => api.post<McpAuthState>(`/library/mcp/${encodeURIComponent(name)}/auth`),
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
