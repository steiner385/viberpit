import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import * as React from 'react'
import { useStartMcpAuth, useMcpAuthStatus, useSubmitMcpRedirect, useCancelMcpAuth } from '../hooks'
import { api } from '../client'

vi.mock('../client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  },
  ApiRequestError: class ApiRequestError extends Error {
    readonly status: number
    readonly body: Record<string, unknown>
    constructor(status: number, body: Record<string, unknown>) {
      super(`API error ${status}`)
      this.name = 'ApiRequestError'
      this.status = status
      this.body = body
    }
  },
}))

const MCP_SUCCESS_STATE = {
  state: 'success' as const,
  server: 'notion',
  message: null,
  authUrl: null,
  accounts: ['tony@example.com'],
  startedAt: 1234567890,
}

const MCP_AWAITING_STATE = {
  state: 'awaiting_redirect' as const,
  server: 'notion',
  message: 'Awaiting redirect',
  authUrl: 'https://api.notion.com/v1/oauth/authorize?...',
  accounts: [],
  startedAt: 1234567890,
}

function makeWrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: qc }, children)
}

describe('useStartMcpAuth', () => {
  beforeEach(() => {
    vi.mocked(api.post).mockResolvedValue(MCP_AWAITING_STATE)
  })

  it('POSTs to /library/mcp/{name}/auth', async () => {
    const { result } = renderHook(() => useStartMcpAuth(), { wrapper: makeWrapper() })

    result.current.mutate('notion')

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(api.post).toHaveBeenCalledWith('/library/mcp/notion/auth')
    expect(result.current.data).toEqual(MCP_AWAITING_STATE)
  })

  it('URL-encodes the server name', async () => {
    const { result } = renderHook(() => useStartMcpAuth(), { wrapper: makeWrapper() })

    result.current.mutate('my server')

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(api.post).toHaveBeenCalledWith('/library/mcp/my%20server/auth')
  })
})

describe('useMcpAuthStatus', () => {
  beforeEach(() => {
    vi.mocked(api.get).mockResolvedValue(MCP_SUCCESS_STATE)
  })

  it('GETs /library/mcp/{name}/auth when enabled', async () => {
    const { result } = renderHook(
      () => useMcpAuthStatus('notion', true),
      { wrapper: makeWrapper() },
    )

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(api.get).toHaveBeenCalledWith('/library/mcp/notion/auth')
    expect(result.current.data).toEqual(MCP_SUCCESS_STATE)
  })

  it('does not fetch when disabled', async () => {
    vi.mocked(api.get).mockClear()

    const { result } = renderHook(
      () => useMcpAuthStatus('notion', false),
      { wrapper: makeWrapper() },
    )

    await new Promise((r) => setTimeout(r, 50))

    expect(result.current.isFetching).toBe(false)
    expect(api.get).not.toHaveBeenCalled()
  })

  it('stops polling on terminal state (success)', async () => {
    vi.mocked(api.get).mockResolvedValue(MCP_SUCCESS_STATE)

    const { result } = renderHook(
      () => useMcpAuthStatus('notion', true),
      { wrapper: makeWrapper() },
    )

    await waitFor(() => expect(result.current.data?.state).toBe('success'))

    // Verify data reached the terminal state
    expect(result.current.data?.state).toBe('success')
  })

  it('stops polling on terminal state (error)', async () => {
    vi.mocked(api.get).mockResolvedValue({ ...MCP_SUCCESS_STATE, state: 'error' })

    const { result } = renderHook(
      () => useMcpAuthStatus('notion', true),
      { wrapper: makeWrapper() },
    )

    await waitFor(() => expect(result.current.data?.state).toBe('error'))
    expect(result.current.data?.state).toBe('error')
  })

  it('stops polling on terminal state (cancelled)', async () => {
    vi.mocked(api.get).mockResolvedValue({ ...MCP_SUCCESS_STATE, state: 'cancelled' })

    const { result } = renderHook(
      () => useMcpAuthStatus('notion', true),
      { wrapper: makeWrapper() },
    )

    await waitFor(() => expect(result.current.data?.state).toBe('cancelled'))
    expect(result.current.data?.state).toBe('cancelled')
  })

  it('URL-encodes the server name', async () => {
    const { result } = renderHook(
      () => useMcpAuthStatus('my server', true),
      { wrapper: makeWrapper() },
    )

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(api.get).toHaveBeenCalledWith('/library/mcp/my%20server/auth')
  })
})

describe('useSubmitMcpRedirect', () => {
  beforeEach(() => {
    vi.mocked(api.post).mockResolvedValue(MCP_SUCCESS_STATE)
    vi.mocked(api.get).mockResolvedValue([])
  })

  it('POSTs to /library/mcp/{name}/auth/redirect with redirectUrl', async () => {
    const { result } = renderHook(() => useSubmitMcpRedirect(), { wrapper: makeWrapper() })

    result.current.mutate({ name: 'notion', redirectUrl: 'https://example.com/callback?code=abc' })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(api.post).toHaveBeenCalledWith(
      '/library/mcp/notion/auth/redirect',
      { redirectUrl: 'https://example.com/callback?code=abc' },
    )
    expect(result.current.data).toEqual(MCP_SUCCESS_STATE)
  })

  it('invalidates libraryMcp query on success', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries')
    const wrapper = ({ children }: { children: React.ReactNode }) =>
      React.createElement(QueryClientProvider, { client: qc }, children)

    const { result } = renderHook(() => useSubmitMcpRedirect(), { wrapper })

    result.current.mutate({ name: 'notion', redirectUrl: 'https://example.com/callback' })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['library', 'mcp'] })
  })
})

describe('useCancelMcpAuth', () => {
  beforeEach(() => {
    vi.mocked(api.post).mockResolvedValue({ ...MCP_SUCCESS_STATE, state: 'cancelled' })
  })

  it('POSTs to /library/mcp/{name}/auth/cancel', async () => {
    const { result } = renderHook(() => useCancelMcpAuth(), { wrapper: makeWrapper() })

    result.current.mutate('notion')

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(api.post).toHaveBeenCalledWith('/library/mcp/notion/auth/cancel')
    expect(result.current.data?.state).toBe('cancelled')
  })

  it('URL-encodes the server name', async () => {
    const { result } = renderHook(() => useCancelMcpAuth(), { wrapper: makeWrapper() })

    result.current.mutate('my server')

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(api.post).toHaveBeenCalledWith('/library/mcp/my%20server/auth/cancel')
  })
})
