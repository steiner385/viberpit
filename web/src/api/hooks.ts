import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState, useRef, useCallback, useSyncExternalStore } from 'react'
import { api, ApiRequestError } from './client'
import type {
  Account, Session, ReauthState, ArchivedSession, AdoptionCandidate, AdoptOutcome, ModelOption, BatchResult,
  DeployStatusResponse, PendingDeploy, DeployProgress, PipelinePr, StageModel,
} from './types'
import { fetchDeployStatus, startDeploy } from './deploy'
import type { SessionEvent } from '@/session/events'
import type { ToolDetailMode } from '@/session/groupBlocks'
import { getPendingSessions, subscribePending, addPendingSession, removePendingSession } from '@/lib/pendingSessions'
import { isSessionsViewing, setSessionsViewing } from './sessionsViewing'
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
}

export function useModels() {
  return useQuery<ModelOption[]>({
    queryKey: QK.models,
    queryFn: () => api.get<ModelOption[]>('/models'),
    staleTime: 5 * 60_000,   // curated list rarely changes
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

export function useGithubRepos() {
  return useQuery<GithubRepo[]>({
    queryKey: QK.githubRepos,
    queryFn: () => api.get<GithubRepo[]>('/github/repos'),
    staleTime: 30_000,
    // The edge serves a last-good cached list on GitHub failure, so this rarely errors;
    // bound retries anyway (default is 3) so a genuine error can't compound across
    // refetch-on-focus and multiple tabs into a request storm.
    retry: 1,
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
  prAutofixMode: string
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
}

export function useCreateSession() {
  const qc = useQueryClient()
  return useMutation<Session, ApiRequestError, CreateSessionInput, { tempId: string }>({
    mutationFn: (input) => api.post<Session>('/sessions', input),
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
      if (ctx?.prev) qc.setQueryData(QK.sessions, ctx.prev)
      toast.error(`Couldn't archive ${ctx?.removed?.name ?? 'session'}`)
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
    onSettled: () => {
      qc.invalidateQueries({ queryKey: QK.sessions })
      qc.invalidateQueries({ queryKey: QK.archived })
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

export function useUpdateAccountNickname() {
  const qc = useQueryClient()
  return useMutation<Account, ApiRequestError, { name: string; nickname: string | null }>({
    mutationFn: ({ name, nickname }) =>
      api.patch<Account>(`/accounts/${encodeURIComponent(name)}`, { nickname }),
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
    onError: (_e, _id, ctx) => {
      if (ctx?.prev) qc.setQueryData(QK.archived, ctx.prev)
      toast.error(`Couldn't restore ${ctx?.removed?.name ?? 'session'}`)
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
    onSuccess: (_d, _id, ctx) => {
      toast.success(`Deleted ${ctx?.name ?? 'session'}`)
    },
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
}

export function useSessionTranscript(id: string, enabled: boolean) {
  return useQuery<SessionTranscript>({
    queryKey: ['session-transcript', id],
    queryFn: () => api.get<SessionTranscript>(`/sessions/${id}/transcript`),
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
  return useMutation<{ started: boolean; reason?: string }, ApiRequestError, void>({
    mutationFn: () => startDeploy(),
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
