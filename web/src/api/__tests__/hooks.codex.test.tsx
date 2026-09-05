/**
 * The engine-aware halves of `@/api/hooks`: the model query's engine/account
 * context, the config route's engine offers, and the OpenAI device-flow trio.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import * as React from 'react'
import {
  QK, useModels, useEngineOffers,
  useAddOpenAIAccount, useOpenAIAddStatus, useCancelOpenAIAdd,
  useOpenAIReauth, useOpenAIReauthStatus, useCancelOpenAIReauth,
} from '../hooks'
import { api } from '../client'
import type { EngineOffer } from '../types'

vi.mock('../client', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), del: vi.fn() },
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

const DEVICE_STATE = {
  state: 'awaiting_authorization' as const,
  userCode: 'ABCD-1234',
  verificationUri: 'https://chatgpt.com/device',
}

function capabilities(over: Partial<EngineOffer['capabilities']> = {}): EngineOffer['capabilities'] {
  return {
    models: true, reasoningEffort: false, steer: true, interrupt: true,
    approvals: true, questions: true, fork: true, adopt: true,
    selfModelSwitch: true, subagents: true, attachments: true, accountRotation: true,
    ...over,
  }
}

function makeWrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: qc }, children)
}

// ── QK.models ────────────────────────────────────────────────────────────────

describe('QK.models', () => {
  it('keys on engine AND account — one shared key would collide the two lists', () => {
    // A single ['models'] key served Codex's account-scoped list to the Claude
    // picker (and back) the moment both engines were offered.
    expect(QK.models('claude_code', null)).not.toEqual(QK.models('codex', 'openai-1'))
    expect(QK.models('codex', 'openai-1')).not.toEqual(QK.models('codex', 'openai-2'))
    expect(QK.models('claude_code', null)).toEqual(QK.models('claude_code', null))
  })
})

// ── useModels ────────────────────────────────────────────────────────────────

describe('useModels', () => {
  beforeEach(() => {
    vi.mocked(api.get).mockReset()
    vi.mocked(api.get).mockResolvedValue([{ id: 'opus', label: 'Opus' }])
  })

  it('keeps the legacy /models URL for Claude, so every existing caller is unchanged', async () => {
    const { result } = renderHook(() => useModels(), { wrapper: makeWrapper() })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(api.get).toHaveBeenCalledWith('/models')
  })

  it('sends engine + account for Codex', async () => {
    const { result } = renderHook(
      () => useModels('codex', 'my account'), { wrapper: makeWrapper() })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(api.get).toHaveBeenCalledWith('/models?engine=codex&account=my%20account')
  })

  it('does NOT fire for Codex without an account (the server answers 400)', async () => {
    const { result } = renderHook(() => useModels('codex', null), { wrapper: makeWrapper() })
    await new Promise((r) => setTimeout(r, 50))
    expect(api.get).not.toHaveBeenCalled()
    expect(result.current.isFetching).toBe(false)
  })
})

// ── useEngineOffers ──────────────────────────────────────────────────────────

describe('useEngineOffers', () => {
  it('reads `engines` off the ONE /config query — no second request', async () => {
    vi.mocked(api.get).mockReset()
    const offers: EngineOffer[] = [
      { engine: 'claude_code', available: true, hosts: ['edge'], unavailableReasons: {}, capabilities: capabilities() },
      { engine: 'codex', available: false, hosts: [], unavailableReasons: { edge: 'binary_missing' }, capabilities: capabilities({ reasoningEffort: true }) },
    ]
    vi.mocked(api.get).mockResolvedValue({ repoRoot: '/r', adoptionEnabled: false, engines: offers })

    const { result } = renderHook(() => useEngineOffers(), { wrapper: makeWrapper() })
    await waitFor(() => expect(result.current).toHaveLength(2))
    expect(api.get).toHaveBeenCalledTimes(1)
    expect(api.get).toHaveBeenCalledWith('/config')
    expect(result.current[1].unavailableReasons).toEqual({ edge: 'binary_missing' })
  })

  it('is an empty list on an edge that predates engine offers', async () => {
    vi.mocked(api.get).mockReset()
    vi.mocked(api.get).mockResolvedValue({ repoRoot: '/r', adoptionEnabled: false })
    const { result } = renderHook(() => useEngineOffers(), { wrapper: makeWrapper() })
    await waitFor(() => expect(api.get).toHaveBeenCalled())
    expect(result.current).toEqual([])
  })
})

// ── OpenAI enrolment (modelled exactly on the Copilot trio) ──────────────────

describe('useAddOpenAIAccount', () => {
  beforeEach(() => { vi.mocked(api.post).mockResolvedValue(DEVICE_STATE) })

  it('POSTs to /accounts/add/openai with { name }', async () => {
    const { result } = renderHook(() => useAddOpenAIAccount(), { wrapper: makeWrapper() })
    result.current.mutate('acc')
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(api.post).toHaveBeenCalledWith('/accounts/add/openai', { name: 'acc' })
    expect(result.current.data).toEqual(DEVICE_STATE)
  })

  it('seeds the new flow state before polling is enabled', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    qc.setQueryData(QK.openaiAddStatus('acc'), { ...DEVICE_STATE, state: 'failed' })
    const wrapper = ({ children }: { children: React.ReactNode }) =>
      React.createElement(QueryClientProvider, { client: qc }, children)

    const { result } = renderHook(() => useAddOpenAIAccount(), { wrapper })
    result.current.mutate('acc')
    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(qc.getQueryData(QK.openaiAddStatus('acc'))).toEqual(DEVICE_STATE)
  })
})

describe('useOpenAIAddStatus', () => {
  beforeEach(() => { vi.mocked(api.get).mockReset(); vi.mocked(api.get).mockResolvedValue(DEVICE_STATE) })

  it('GETs /accounts/add/openai?name=<name>, URL-encoded, when enabled', async () => {
    const { result } = renderHook(
      () => useOpenAIAddStatus('my account', true), { wrapper: makeWrapper() })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(api.get).toHaveBeenCalledWith('/accounts/add/openai?name=my%20account')
  })

  it('does not fetch when disabled', async () => {
    const { result } = renderHook(() => useOpenAIAddStatus('acc', false), { wrapper: makeWrapper() })
    await new Promise((r) => setTimeout(r, 50))
    expect(result.current.isFetching).toBe(false)
    expect(api.get).not.toHaveBeenCalled()
  })

  it('stops polling at the terminal states', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: React.ReactNode }) =>
      React.createElement(QueryClientProvider, { client: qc }, children)
    vi.mocked(api.get).mockResolvedValue({ ...DEVICE_STATE, state: 'authorized' })
    const { result } = renderHook(() => useOpenAIAddStatus('acc', true), { wrapper })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    const observer = qc.getQueryCache().find({ queryKey: ['openai-add-status', 'acc'] })
    const interval = observer?.observers[0]?.options.refetchInterval
    expect(typeof interval === 'function' ? interval(observer!) : interval).toBe(false)
  })
})

describe('useCancelOpenAIAdd', () => {
  it('POSTs the cancel and invalidates the accounts list', async () => {
    vi.mocked(api.post).mockResolvedValue({ state: 'failed', userCode: null, verificationUri: null })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    const wrapper = ({ children }: { children: React.ReactNode }) =>
      React.createElement(QueryClientProvider, { client: qc }, children)

    const { result } = renderHook(() => useCancelOpenAIAdd(), { wrapper })
    result.current.mutate('acc')
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(api.post).toHaveBeenCalledWith('/accounts/add/openai/cancel', { name: 'acc' })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: QK.accounts })
  })
})

// ── OpenAI reauth (the SAME device flow, on the per-account routes) ──────────

describe('OpenAI reauth', () => {
  beforeEach(() => {
    vi.mocked(api.get).mockReset()
    vi.mocked(api.get).mockResolvedValue(DEVICE_STATE)
    vi.mocked(api.post).mockResolvedValue(DEVICE_STATE)
  })

  it('starts, polls and cancels on /accounts/{name}/reauth', async () => {
    const wrapper = makeWrapper()
    const start = renderHook(() => useOpenAIReauth(), { wrapper })
    start.result.current.mutate('my account')
    await waitFor(() => expect(start.result.current.isSuccess).toBe(true))
    expect(api.post).toHaveBeenCalledWith('/accounts/my%20account/reauth')

    const status = renderHook(() => useOpenAIReauthStatus('my account', true), { wrapper })
    await waitFor(() => expect(status.result.current.isSuccess).toBe(true))
    expect(api.get).toHaveBeenCalledWith('/accounts/my%20account/reauth')

    const cancel = renderHook(() => useCancelOpenAIReauth(), { wrapper })
    cancel.result.current.mutate('my account')
    await waitFor(() => expect(cancel.result.current.isSuccess).toBe(true))
    expect(api.post).toHaveBeenCalledWith('/accounts/my%20account/reauth/cancel')
  })

  it('replaces a previous terminal state before reauth polling is enabled', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    qc.setQueryData(QK.openaiReauthStatus('acc'), { ...DEVICE_STATE, state: 'failed' })
    const wrapper = ({ children }: { children: React.ReactNode }) =>
      React.createElement(QueryClientProvider, { client: qc }, children)

    const { result } = renderHook(() => useOpenAIReauth(), { wrapper })
    result.current.mutate('acc')
    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(qc.getQueryData(QK.openaiReauthStatus('acc'))).toEqual(DEVICE_STATE)
  })

  it('keys its poll apart from the Anthropic reauth status of the same account', async () => {
    // Same URL, different payload shape: sharing ['reauth-status', name] would
    // hand a ReauthState reader a device-code snapshot and vice versa.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: React.ReactNode }) =>
      React.createElement(QueryClientProvider, { client: qc }, children)
    const { result } = renderHook(() => useOpenAIReauthStatus('acc', true), { wrapper })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(qc.getQueryCache().find({ queryKey: ['reauth-status', 'acc'] })).toBeUndefined()
    expect(qc.getQueryCache().find({ queryKey: ['openai-reauth-status', 'acc'] })).toBeDefined()
  })
})
