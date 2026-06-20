import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiRequestError } from './client'
import type { Account, Session } from './types'

// Query keys
export const QK = {
  accounts: ['accounts'] as const,
  sessions: ['sessions'] as const,
  config: ['config'] as const,
  githubRepos: ['github-repos'] as const,
  githubBranches: (repo?: string) => ['github-branches', repo] as const,
  settings: ['settings'] as const,
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
}

export function useCreateSession() {
  const qc = useQueryClient()
  return useMutation<Session, ApiRequestError, CreateSessionInput>({
    mutationFn: (input) => api.post<Session>('/sessions', input),
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.sessions }) },
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
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.sessions }) },
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
