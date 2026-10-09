import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import * as React from 'react'
import {
  QK,
  useMcpScope,
  useSetMcpDefault,
  useSetMcpRepoRule,
  useDeleteMcpRepoRule,
  useToggleSessionMcp,
} from '../hooks'
import { api } from '../client'

vi.mock('../client', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), del: vi.fn() },
  ApiRequestError: class ApiRequestError extends Error {},
}))

function setup() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: qc }, children)
  return { qc, wrapper }
}

beforeEach(() => vi.clearAllMocks())

describe('MCP scope hooks', () => {
  it('useMcpScope GETs /library/mcp/scope', async () => {
    const scope = { servers: { notion: { defaultEnabled: true } }, rules: [] }
    vi.mocked(api.get).mockResolvedValue(scope)
    const { wrapper } = setup()
    const { result } = renderHook(() => useMcpScope(), { wrapper })
    await waitFor(() => expect(result.current.data).toEqual(scope))
    expect(api.get).toHaveBeenCalledWith('/library/mcp/scope')
  })

  it('useSetMcpDefault PUTs the encoded server key and invalidates the scope', async () => {
    vi.mocked(api.put).mockResolvedValue(undefined)
    const { qc, wrapper } = setup()
    const spy = vi.spyOn(qc, 'invalidateQueries')
    const { result } = renderHook(() => useSetMcpDefault(), { wrapper })
    result.current.mutate({ server: 'plugin:p:s', defaultEnabled: false })
    await waitFor(() => expect(spy).toHaveBeenCalledWith({ queryKey: QK.mcpScope }))
    expect(api.put).toHaveBeenCalledWith('/library/mcp/scope/plugin%3Ap%3As', { defaultEnabled: false })
  })

  it('useSetMcpRepoRule splits the repo into encoded owner/name', async () => {
    vi.mocked(api.put).mockResolvedValue(undefined)
    const { qc, wrapper } = setup()
    const spy = vi.spyOn(qc, 'invalidateQueries')
    const { result } = renderHook(() => useSetMcpRepoRule(), { wrapper })
    result.current.mutate({ server: 'plugin:p:s', repo: 'acme/web.app', enabled: true })
    await waitFor(() => expect(spy).toHaveBeenCalledWith({ queryKey: QK.mcpScope }))
    expect(api.put).toHaveBeenCalledWith('/library/mcp/scope/plugin%3Ap%3As/repos/acme/web.app', { enabled: true })
  })

  it('useDeleteMcpRepoRule DELETEs the rule path and invalidates', async () => {
    vi.mocked(api.del).mockResolvedValue(undefined)
    const { qc, wrapper } = setup()
    const spy = vi.spyOn(qc, 'invalidateQueries')
    const { result } = renderHook(() => useDeleteMcpRepoRule(), { wrapper })
    result.current.mutate({ server: 'notion', repo: 'acme/web' })
    await waitFor(() => expect(spy).toHaveBeenCalledWith({ queryKey: QK.mcpScope }))
    expect(api.del).toHaveBeenCalledWith('/library/mcp/scope/notion/repos/acme/web')
  })

  it('useToggleSessionMcp POSTs and writes the returned servers into the session cache', async () => {
    const data = { servers: [{ name: 'notion', status: 'connected' }] }
    vi.mocked(api.post).mockResolvedValue(data)
    const { qc, wrapper } = setup()
    const { result } = renderHook(() => useToggleSessionMcp('s 1'), { wrapper })
    result.current.mutate({ server: 'plugin:p:s', enabled: false })
    await waitFor(() => expect(qc.getQueryData(['session-mcp', 's 1'])).toEqual(data))
    expect(api.post).toHaveBeenCalledWith('/sessions/s%201/mcp/plugin%3Ap%3As/toggle', { enabled: false })
  })
})
