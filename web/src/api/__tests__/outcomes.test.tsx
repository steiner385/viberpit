/**
 * Dispatch outcome fetcher + hook (product automation policy §6/§7; server
 * contract `server/api/pm.py:get_work_item_outcomes`, `server/pm/outcomes.py`).
 * Pins the wire shape the roadmap's bounce chip reads history from.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

vi.mock('@/api/client', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))

import { api } from '@/api/client'
import { fetchOutcomes } from '@/api/pm'
import { useOutcomes } from '@/api/hooks'

const get = vi.mocked(api.get)

function wrap() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

const row = {
  id: 'o1', targetRef: 'o/r#1', projectId: 'p1', sessionId: 's1', model: 'opus',
  attemptNo: 1, outcome: 'no_pr', cause: 'stale_branch', prNumber: null, prUrl: null,
  firstPassMerge: null, bodyFingerprint: 'fp1', startedAt: '2026-09-12T00:00:00Z',
  endedAt: '2026-09-12T00:05:00Z', durationS: 300, createdAt: '2026-09-12T00:00:00Z',
}

describe('fetchOutcomes', () => {
  beforeEach(() => get.mockReset())

  it('reads one work item history and unwraps items', async () => {
    get.mockResolvedValueOnce({ items: [row] })
    await expect(fetchOutcomes('w 1')).resolves.toEqual([row])
    expect(get).toHaveBeenCalledWith('/pm/work-items/w%201/outcomes')
  })
})

describe('useOutcomes', () => {
  beforeEach(() => get.mockReset())

  it('calls the outcomes endpoint and returns the items', async () => {
    get.mockResolvedValueOnce({ items: [row] })
    const { result } = renderHook(() => useOutcomes('w1'), { wrapper: wrap() })
    await waitFor(() => expect(result.current.data).toEqual([row]))
    expect(get).toHaveBeenCalledWith('/pm/work-items/w1/outcomes')
  })

  it('does not fetch while disabled', async () => {
    renderHook(() => useOutcomes('w1', { enabled: false }), { wrapper: wrap() })
    await new Promise((r) => setTimeout(r, 20))
    expect(get).not.toHaveBeenCalled()
  })
})
