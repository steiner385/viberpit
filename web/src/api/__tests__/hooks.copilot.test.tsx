import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import * as React from 'react'
import { useAddCopilotAccount, useCopilotAddStatus, useCancelCopilotAdd } from '../hooks'
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

const COPILOT_STATE = {
  state: 'awaiting_authorization' as const,
  userCode: 'ABCD-1234',
  verificationUri: 'https://github.com/login/device',
}

function makeWrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: qc }, children)
}

describe('useAddCopilotAccount', () => {
  beforeEach(() => {
    vi.mocked(api.post).mockResolvedValue(COPILOT_STATE)
  })

  it('POSTs to /accounts/add/copilot with { name }', async () => {
    const { result } = renderHook(() => useAddCopilotAccount(), { wrapper: makeWrapper() })

    result.current.mutate('acc')

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(api.post).toHaveBeenCalledWith('/accounts/add/copilot', { name: 'acc' })
    expect(result.current.data).toEqual(COPILOT_STATE)
  })
})

describe('useCopilotAddStatus', () => {
  beforeEach(() => {
    vi.mocked(api.get).mockResolvedValue(COPILOT_STATE)
  })

  it('GETs /accounts/add/copilot?name=<name> when enabled', async () => {
    const { result } = renderHook(
      () => useCopilotAddStatus('acc', true),
      { wrapper: makeWrapper() },
    )

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(api.get).toHaveBeenCalledWith('/accounts/add/copilot?name=acc')
    expect(result.current.data).toEqual(COPILOT_STATE)
  })

  it('does not fetch when disabled', async () => {
    vi.mocked(api.get).mockClear()

    const { result } = renderHook(
      () => useCopilotAddStatus('acc', false),
      { wrapper: makeWrapper() },
    )

    // Give it a tick to (not) fetch
    await new Promise((r) => setTimeout(r, 50))

    expect(result.current.isFetching).toBe(false)
    expect(api.get).not.toHaveBeenCalled()
  })

  it('URL-encodes the name', async () => {
    const { result } = renderHook(
      () => useCopilotAddStatus('my account', true),
      { wrapper: makeWrapper() },
    )

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(api.get).toHaveBeenCalledWith('/accounts/add/copilot?name=my%20account')
  })
})

describe('useCancelCopilotAdd', () => {
  beforeEach(() => {
    vi.mocked(api.post).mockResolvedValue({ state: 'failed', userCode: null, verificationUri: null })
    vi.mocked(api.get).mockResolvedValue([])
  })

  it('POSTs to /accounts/add/copilot/cancel with { name }', async () => {
    const { result } = renderHook(() => useCancelCopilotAdd(), { wrapper: makeWrapper() })

    result.current.mutate('acc')

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(api.post).toHaveBeenCalledWith('/accounts/add/copilot/cancel', { name: 'acc' })
  })

  it('invalidates accounts query on success', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries')
    const wrapper = ({ children }: { children: React.ReactNode }) =>
      React.createElement(QueryClientProvider, { client: qc }, children)

    const { result } = renderHook(() => useCancelCopilotAdd(), { wrapper })

    result.current.mutate('acc')

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['accounts'] })
  })
})
