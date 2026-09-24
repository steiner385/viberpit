import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import * as React from 'react'
import { QK, useSettings, useUpdateSettings, useFleetHostPatch } from '../hooks'
import { FLEET_HOSTS_KEY } from '@/api/fleet'
import { api } from '../client'
import { appSettings } from '@/test/factories'

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

function makeWrapper(qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
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

  it('exposes defaultPermissionMode, thinkingCollapsed, repoRoot, and defaultToolDetail', async () => {
    const { result } = renderHook(() => useSettings(), { wrapper: makeWrapper() })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(result.current.data?.defaultPermissionMode).toBe('interactive')
    expect(result.current.data?.thinkingCollapsed).toBe(false)
    expect(result.current.data?.repoRoot).toBe('/home/user/repos')
    expect(result.current.data?.defaultToolDetail).toBe('auto')
  })

  it('exposes the server blocks added for Settings (spec §9)', async () => {
    const payload = appSettings({
      acceptedPermissionModes: ['interactive'],
      effectiveDefaults: {
        repoRoot: '/r',
        archiveHome: null,
        modelApprovalAction: 'block',
        modelApprovalDenylist: 'fable',
        globalCap: { physical: null, attention: null, effective: 5, binding: 'default', hosts: [] },
      },
      managed: [{ key: 'VIBERPIT_ENABLE_COPILOT', value: false, description: 'Enable GitHub Copilot accounts' }],
      about: { gitSha: 'abc1234', hostName: 'dobby' },
    })
    vi.mocked(api.get).mockResolvedValueOnce(payload)

    const { result } = renderHook(() => useSettings(), { wrapper: makeWrapper() })

    await waitFor(() => expect(result.current.data).toBeDefined())

    expect(result.current.data?.effectiveDefaults.modelApprovalAction).toBe('block')
    expect(result.current.data?.about.hostName).toBe('dobby')
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

  it('a settings save invalidates the capacity readout too (spec 2026-09-20 C20)', async () => {
    vi.mocked(api.put).mockResolvedValueOnce(appSettings())
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const spy = vi.spyOn(qc, 'invalidateQueries')
    const { result } = renderHook(() => useUpdateSettings(), { wrapper: makeWrapper(qc) })
    await act(async () => { await result.current.mutateAsync({ maxActiveSessions: 3 }) })
    expect(spy).toHaveBeenCalledWith({ queryKey: QK.settings })
    expect(spy).toHaveBeenCalledWith({ queryKey: QK.capacity })
  })
})

describe('useFleetHostPatch', () => {
  it('invalidates the fleet, capacity AND settings query keys on success (F2)', async () => {
    // A host cap edit changes effectiveDefaults.globalCap, which rides on GET
    // /api/settings (the Hosts footer and the Limits rows both read it) — so
    // this mutation must invalidate QK.settings too, or the "Σ caps = N"
    // readout goes stale after editing a host cap.
    vi.mocked(api.put).mockResolvedValueOnce({ name: 'rocky', maxSessions: 8 })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const spy = vi.spyOn(qc, 'invalidateQueries')
    const { result } = renderHook(() => useFleetHostPatch(), { wrapper: makeWrapper(qc) })

    await act(async () => {
      await result.current.mutateAsync({ name: 'rocky', body: { maxSessions: 8 } })
    })

    expect(spy).toHaveBeenCalledWith({ queryKey: FLEET_HOSTS_KEY })
    expect(spy).toHaveBeenCalledWith({ queryKey: QK.capacity })
    expect(spy).toHaveBeenCalledWith({ queryKey: QK.settings })
  })
})
