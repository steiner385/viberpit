import { renderHook, waitFor } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

vi.mock('@/api/client', () => ({ api: { get: vi.fn(), post: vi.fn() } }))
import { api } from '@/api/client'
import {
  useAssistantHistory, useAssistantSession, useAssistantStatus, useResumeAssistantSession,
} from './assistant'

const wrap = () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('assistant api', () => {
  it('reads status and creates a session', async () => {
    vi.mocked(api.get).mockResolvedValue({ enabled: true, sessionId: null, model: 'sonnet' })
    vi.mocked(api.post).mockResolvedValue({ id: 'a1', model: 'sonnet', state: 'created' })
    const wrapper = wrap()
    const { result } = renderHook(() => useAssistantStatus(), { wrapper })
    await waitFor(() => expect(result.current.data?.enabled).toBe(true))
    const m = renderHook(() => useAssistantSession(), { wrapper })
    const out = await m.result.current.mutateAsync({})
    expect(out.id).toBe('a1')
    expect(api.post).toHaveBeenCalledWith('/assistant/session', {})
  })

  it('fetches history only while enabled', async () => {
    vi.mocked(api.get).mockResolvedValue([{ id: 'h1', snippet: 'fix the thing', archivedAt: '2026-09-17T00:00:00Z' }])
    const historyCalls = () => vi.mocked(api.get).mock.calls.filter((c) => c[0] === '/assistant/history').length
    const wrapper = wrap()
    const disabled = renderHook(() => useAssistantHistory(false), { wrapper })
    expect(disabled.result.current.fetchStatus).toBe('idle')
    expect(historyCalls()).toBe(0)

    const enabled = renderHook(() => useAssistantHistory(true), { wrapper })
    await waitFor(() => expect(enabled.result.current.data).toHaveLength(1))
    expect(historyCalls()).toBe(1)
    expect(enabled.result.current.data?.[0].id).toBe('h1')
  })

  it('resumes a thread and invalidates status + history', async () => {
    vi.mocked(api.post).mockResolvedValue({ id: 'h1', model: 'sonnet', engine: 'claude_code', state: 'resumed' })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    qc.setQueryData(['assistant', 'status'], { enabled: true, sessionId: 'live1', model: 'sonnet' })
    qc.setQueryData(['assistant', 'history'], [{ id: 'h1', snippet: null, archivedAt: null }])
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    )
    const { result } = renderHook(() => useResumeAssistantSession(), { wrapper })
    const out = await result.current.mutateAsync('h1')
    expect(out.state).toBe('resumed')
    expect(api.post).toHaveBeenCalledWith('/assistant/session/h1/resume')
    await waitFor(() => {
      expect(qc.getQueryState(['assistant', 'status'])?.isInvalidated).toBe(true)
      expect(qc.getQueryState(['assistant', 'history'])?.isInvalidated).toBe(true)
    })
  })
})
