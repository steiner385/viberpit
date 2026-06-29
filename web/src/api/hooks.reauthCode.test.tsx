/**
 * Verifies:
 * 1. useSubmitReauthCode POSTs to /accounts/<name>/reauth/code with { code }.
 * 2. useReauthAccount with { name, mode: 'setup_token' } hits /…/reauth?mode=setup_token.
 * 3. useReauthAccount with a bare string hits /…/reauth (no query param).
 * 4. useReauthAccount with { name } and no mode hits /…/reauth (no query param).
 *
 * Strategy: mock api.post to resolve, run the mutations, assert the call shape.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import React from 'react'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useSubmitReauthCode, useReauthAccount } from './hooks'

vi.mock('./client', () => ({
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
  api: {
    post: vi.fn().mockResolvedValue({ state: 'running' }),
    get: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  },
}))

import { api } from './client'

function makeWrapper() {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.post).mockResolvedValue({ state: 'running' })
})

describe('useSubmitReauthCode', () => {
  it('POSTs to /accounts/<name>/reauth/code with the code in the body', async () => {
    const wrapper = makeWrapper()
    const { result } = renderHook(() => useSubmitReauthCode(), { wrapper })

    result.current.mutate({ name: 'kindash', code: 'abc123' })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(api.post).toHaveBeenCalledWith(
      '/accounts/kindash/reauth/code',
      { code: 'abc123' },
    )
  })

  it('URL-encodes special characters in the account name', async () => {
    const wrapper = makeWrapper()
    const { result } = renderHook(() => useSubmitReauthCode(), { wrapper })

    result.current.mutate({ name: 'foo/bar', code: 'xyz' })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(api.post).toHaveBeenCalledWith(
      '/accounts/foo%2Fbar/reauth/code',
      { code: 'xyz' },
    )
  })
})

describe('useReauthAccount — mode param', () => {
  it('hits /reauth?mode=setup_token when called with { name, mode: "setup_token" }', async () => {
    const wrapper = makeWrapper()
    const { result } = renderHook(() => useReauthAccount(), { wrapper })

    result.current.mutate({ name: 'kindash', mode: 'setup_token' })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(api.post).toHaveBeenCalledWith('/accounts/kindash/reauth?mode=setup_token')
  })

  it('hits /reauth (no query param) when called with a bare string', async () => {
    const wrapper = makeWrapper()
    const { result } = renderHook(() => useReauthAccount(), { wrapper })

    result.current.mutate('kindash')

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(api.post).toHaveBeenCalledWith('/accounts/kindash/reauth')
  })

  it('hits /reauth (no query param) when called with { name } and no mode', async () => {
    const wrapper = makeWrapper()
    const { result } = renderHook(() => useReauthAccount(), { wrapper })

    result.current.mutate({ name: 'kindash' })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(api.post).toHaveBeenCalledWith('/accounts/kindash/reauth')
  })
})
