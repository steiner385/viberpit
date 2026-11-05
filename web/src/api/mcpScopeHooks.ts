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

// D-16b per-tool switches. Neither is optimistic: the switch shows the server's state, so a
// refusal (403 human-only, 409 SETTINGS_FAILED, …) leaves it where it was and the caller shows
// the server's error text.
const toolPath = (server: string, tool: string) =>
  `/mcp/${encodeURIComponent(server)}/tools/${encodeURIComponent(tool)}`

// useSetMcpToolDefault: a tool's Settings default (new and restarted Claude sessions).
export function useSetMcpToolDefault() {
  const qc = useQueryClient()
  return useMutation<unknown, ApiRequestError, { server: string; tool: string; enabled: boolean }>({
    mutationFn: ({ server, tool, enabled }) => api.put(`/library${toolPath(server, tool)}`, { enabled }),
    // The panel renders the refusal inline; a toast would say it twice.
    meta: { suppressErrorToast: true },
    onSuccess: (_data, { server, tool, enabled }) => {
      // The PUT answers 204: write the accepted state into the cache, then re-read it.
      qc.setQueryData<McpScope>(QK.mcpScope, (prev) => {
        const entry = prev?.servers?.[server]
        if (!prev || !entry?.tools) return prev
        const tools = entry.tools.map((t) => (t.name === tool ? { ...t, enabled } : t))
        return { ...prev, servers: { ...prev.servers, [server]: { ...entry, tools } } }
      })
      void qc.invalidateQueries({ queryKey: QK.mcpScope })
    },
  })
}

// useSetSessionMcpTool: a live session's override for one tool; the response is the fresh server list.
export function useSetSessionMcpTool(sessionId: string) {
  const qc = useQueryClient()
  return useMutation<{ servers: McpServerState[] }, ApiRequestError, { server: string; tool: string; enabled: boolean }>({
    mutationFn: ({ server, tool, enabled }) =>
      api.post(`/sessions/${encodeURIComponent(sessionId)}${toolPath(server, tool)}`, { enabled }),
    meta: { suppressErrorToast: true },
    onSuccess: (data) => qc.setQueryData(['session-mcp', sessionId], data),
  })
}
