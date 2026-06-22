import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import * as React from 'react'
import { useSettings, useUpdateSettings } from '../hooks'
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

const SETTINGS = {
  defaultPermissionMode: 'interactive',
  thinkingCollapsed: false,
  repoRoot: '/home/user/repos',
  defaultToolDetail: 'auto',
}

function makeWrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: qc }, children)
}

describe('useSettings', () => {
  beforeEach(() => {
    vi.mocked(api.get).mockResolvedValue(SETTINGS)
  })

  it('fetches settings from /settings (not /api/settings — that would double-prefix to /api/api/settings)', async () => {
    const { result } = renderHook(() => useSettings(), { wrapper: makeWrapper() })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(api.get).toHaveBeenCalledWith('/settings')
    expect(result.current.data).toEqual(SETTINGS)
  })

  it('exposes defaultPermissionMode, thinkingCollapsed, and repoRoot', async () => {
    const { result } = renderHook(() => useSettings(), { wrapper: makeWrapper() })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(result.current.data?.defaultPermissionMode).toBe('interactive')
    expect(result.current.data?.thinkingCollapsed).toBe(false)
    expect(result.current.data?.repoRoot).toBe('/home/user/repos')
    expect(result.current.data?.defaultToolDetail).toBe('auto')
  })
})

describe('useUpdateSettings', () => {
  beforeEach(() => {
    vi.mocked(api.put).mockResolvedValue({ ...SETTINGS, thinkingCollapsed: true })
    vi.mocked(api.get).mockResolvedValue(SETTINGS)
  })

  it('calls PUT /settings with the given body (not /api/settings — that would double-prefix to /api/api/settings)', async () => {
    const { result } = renderHook(() => useUpdateSettings(), { wrapper: makeWrapper() })

    result.current.mutate({ thinkingCollapsed: true })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(api.put).toHaveBeenCalledWith('/settings', { thinkingCollapsed: true })
  })

  it('invalidates the settings query key on success', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries')
    const wrapper = ({ children }: { children: React.ReactNode }) =>
      React.createElement(QueryClientProvider, { client: qc }, children)

    const { result } = renderHook(() => useUpdateSettings(), { wrapper })

    result.current.mutate({ defaultPermissionMode: 'auto' })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['settings'] })
  })
})
