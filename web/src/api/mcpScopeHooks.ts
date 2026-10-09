// MCP scope + live-toggle hooks. Kept out of hooks.ts so they ride the lazy Settings and
// in-session MCP readout chunks instead of the critical-path bundle (bundle budget, R7).
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api, type ApiRequestError } from './client'
import type { McpScope, McpServerState } from './types'
import { QK } from './hooks'

export function useMcpScope() {
  return useQuery<McpScope>({
    queryKey: QK.mcpScope,
    queryFn: () => api.get('/library/mcp/scope'),
    staleTime: 30_000,
  })
}

// `repo` is "owner/name"; each segment is encoded, the slash stays a path separator.
const mcpRulePath = (server: string, repo: string) =>
  `/library/mcp/scope/${encodeURIComponent(server)}/repos/${repo.split('/').map(encodeURIComponent).join('/')}`

export function useSetMcpDefault() {
  const qc = useQueryClient()
  return useMutation<unknown, ApiRequestError, { server: string; defaultEnabled: boolean }>({
    mutationFn: ({ server, defaultEnabled }) =>
      api.put(`/library/mcp/scope/${encodeURIComponent(server)}`, { defaultEnabled }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: QK.mcpScope }),
  })
}

export function useSetMcpRepoRule() {
  const qc = useQueryClient()
  return useMutation<unknown, ApiRequestError, { server: string; repo: string; enabled: boolean }>({
    mutationFn: ({ server, repo, enabled }) => api.put(mcpRulePath(server, repo), { enabled }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: QK.mcpScope }),
  })
}

export function useDeleteMcpRepoRule() {
  const qc = useQueryClient()
  return useMutation<unknown, ApiRequestError, { server: string; repo: string }>({
    mutationFn: ({ server, repo }) => api.del(mcpRulePath(server, repo)),
    onSuccess: () => void qc.invalidateQueries({ queryKey: QK.mcpScope }),
  })
}

// useToggleSessionMcp: enables/disables one MCP server inside a live session; the response is the fresh server list.
export function useToggleSessionMcp(sessionId: string) {
  const qc = useQueryClient()
  return useMutation<{ servers: McpServerState[] }, ApiRequestError, { server: string; enabled: boolean }>({
    mutationFn: ({ server, enabled }) =>
      api.post(`/sessions/${encodeURIComponent(sessionId)}/mcp/${encodeURIComponent(server)}/toggle`, { enabled }),
    onSuccess: (data) => qc.setQueryData(['session-mcp', sessionId], data),
  })
}
