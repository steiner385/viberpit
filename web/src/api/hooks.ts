import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiRequestError } from './client'
import type { Account, Session } from './types'

// Query keys
export const QK = {
  accounts: ['accounts'] as const,
  sessions: ['sessions'] as const,
  config: ['config'] as const,
  repos: (root?: string) => ['repos', root] as const,
  branches: (path?: string) => ['branches', path] as const,
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

export interface RepoInfo {
  name: string
  path: string
  currentBranch: string | null
}

export interface BranchInfo {
  current: string | null
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

export function useRepos(root?: string) {
  return useQuery<RepoInfo[]>({
    queryKey: QK.repos(root),
    queryFn: () => api.get<RepoInfo[]>(`/repos${root ? `?root=${encodeURIComponent(root)}` : ''}`),
    enabled: true,
    staleTime: 10_000,
  })
}

export function useBranches(repoPath?: string) {
  return useQuery<BranchInfo>({
    queryKey: QK.branches(repoPath),
    queryFn: () => api.get<BranchInfo>(`/repos/branches?path=${encodeURIComponent(repoPath!)}`),
    enabled: !!repoPath,
    staleTime: 10_000,
  })
}

export interface CreateSessionInput {
  name?: string
  account?: string
  initialPrompt?: string
  cwd?: string
  branch?: string
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
