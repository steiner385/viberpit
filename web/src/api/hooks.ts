import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiRequestError } from './client'
import type {
  Account, Session, ReauthState, ArchivedSession, AdoptionCandidate, AdoptOutcome, ModelOption,
} from './types'
import type { SessionEvent } from '@/session/events'
import type { ToolDetailMode } from '@/session/groupBlocks'

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
  return useQuery<Session[]>({
    queryKey: QK.sessions,
    queryFn: () => api.get<Session[]>('/sessions'),
    staleTime: 3_000,
    refetchInterval: 5_000,
  })
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
  return useMutation<Session, ApiRequestError, CreateSessionInput>({
    mutationFn: (input) => api.post<Session>('/sessions', input),
    onSuccess: (session) => {
      // Show the new card immediately instead of waiting up to 5s for the poll.
      qc.setQueryData<Session[]>(QK.sessions, (prev) => {
        const list = prev ?? []
        if (list.some((s) => s.id === session.id)) return list
        return [...list, session]
      })
      qc.invalidateQueries({ queryKey: QK.sessions })
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

export function useCloseSession() {
  const qc = useQueryClient()
  return useMutation<void, ApiRequestError, string>({
    mutationFn: (id) => api.del<void>(`/sessions/${id}`),
    onSuccess: () => {
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

export function useReauthAccount() {
  return useMutation<ReauthState, ApiRequestError, string>({
    mutationFn: (name) => api.post<ReauthState>(`/accounts/${encodeURIComponent(name)}/reauth`),
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
  })
}

export function useDeleteAccount() {
  const qc = useQueryClient()
  return useMutation<void, ApiRequestError, string>({
    mutationFn: (name) => api.del<void>(`/accounts/${encodeURIComponent(name)}`),
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

export function useUnarchiveSession() {
  const qc = useQueryClient()
  return useMutation<ArchivedSession, ApiRequestError, string>({
    mutationFn: (id) => api.post<ArchivedSession>(`/sessions/${id}/unarchive`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.sessions })
      qc.invalidateQueries({ queryKey: QK.archived })
    },
  })
}

export function usePermanentDeleteSession() {
  const qc = useQueryClient()
  return useMutation<void, ApiRequestError, string>({
    mutationFn: (id) => api.del<void>(`/sessions/${id}/permanent`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.archived })
      // A live session can also be permanently deleted from its card, so the
      // active sessions list must refresh too.
      qc.invalidateQueries({ queryKey: QK.sessions })
    },
  })
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
