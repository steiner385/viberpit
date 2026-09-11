import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { ...actual, api: { ...actual.api, get: vi.fn(async (path: string): Promise<unknown> => {
    // Guarded rather than a bare `path.endsWith`: confirmed by removing this
    // line and rerunning — `api.get` is still called with an empty-string
    // path, every time, but only from inside React Testing Library's
    // automatic unmount cleanup (the stack lands in `callCleanupHooks`, after
    // a test's own assertions have already run), not from any query this
    // file awaits. Which hook fires it was not identified. Harmless in
    // itself, but an un-guarded `.endsWith` on that call's path throws and
    // fails whichever test is mid-teardown at that moment.
    if (!path) return { repo: 'o/r', window: '7d', slowest: [], flakiest: [], regressions: [], runner_wait_health: [] }
    if (path.endsWith('/actionable')) return path.includes('pr-doctor') ? [{ id: 'o/r#1', title: 't', actions: [] }] : []
    if (path === '/pipeline/repos') return { 'o/r': { repo: 'o/r', nodes: [] } }
    return { repo: 'o/r', window: '7d', slowest: [], flakiest: [], regressions: [], runner_wait_health: [] }
  }) } }
})
import { api } from '@/api/client'
import { useAutomationActionableCounts, useCheckInsights, useStageModels } from '@/api/hooks'

const get = vi.mocked(api.get)
function wrap() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('delivery hooks', () => {
  beforeEach(() => get.mockClear())
  it('useAutomationActionableCounts fetches one /actionable per id and counts', async () => {
    const { result } = renderHook(() => useAutomationActionableCounts(['pr-doctor', 'backlog-runner']), { wrapper: wrap() })
    await waitFor(() => expect(result.current).toEqual({ 'pr-doctor': 1, 'backlog-runner': 0 }))
    expect(get.mock.calls.map(([p]) => p).sort()).toEqual(['/automations/backlog-runner/actionable', '/automations/pr-doctor/actionable'])
  })
  it('useAutomationActionableCounts asks for nothing while disabled', async () => {
    const { result } = renderHook(() => useAutomationActionableCounts(['pr-doctor'], false), { wrapper: wrap() })
    expect(result.current).toEqual({ 'pr-doctor': 0 })
    await new Promise((r) => setTimeout(r, 20))
    expect(get).not.toHaveBeenCalled()
  })
  it('useStageModels honours enabled', async () => {
    renderHook(() => useStageModels({ enabled: false }), { wrapper: wrap() })
    await new Promise((r) => setTimeout(r, 20))
    expect(get).not.toHaveBeenCalled()
  })
  it('useCheckInsights fetches the repo+window once without a poll timer', async () => {
    const { result } = renderHook(() => useCheckInsights('o/r', '7d', { poll: false }), { wrapper: wrap() })
    await waitFor(() => expect(result.current.data?.repo).toBe('o/r'))
    expect(get).toHaveBeenCalledWith('/pipeline/insights/checks?repo=o%2Fr&window=7d')
  })
})
