import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { useOnboarding, useTriage } from '@/api/hooks'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

vi.mock('@/api/pm', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/pm')>()),
  fetchOnboarding: vi.fn().mockResolvedValue({
    state: 'map_merged',
    action: { type: 'classify' },
    counts: { repoCount: 1, backfillDoneCount: 1, featureCount: 4,
              openUnclassifiedCount: 30, pendingProposalCount: 0 },
    mapPr: null,
  }),
  advanceMap: vi.fn(),
  advanceLive: vi.fn(),
  requestProjectSync: vi.fn(),
}))

vi.mock('@/api/triage', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/triage')>()),
  fetchTriage: vi.fn().mockResolvedValue([{
    id: 'p1', workItemId: 'w1', repo: 'o/r', number: 7, kind: 'issue',
    title: 'Bug', url: null, proposedFeatureId: 'f1',
    proposedFeatureSlug: 'sessions', noFitSuggestion: null,
    reach: 3, impact: 2, effort: 1, confidence: 2, rationale: 'fits',
    confidenceLevel: 'high', status: 'pending', createdAt: null,
  }]),
}))

function wrap() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
}

describe('pm hooks', () => {
  it('useOnboarding returns the engine view', async () => {
    const { result } = renderHook(() => useOnboarding('ccswitch'), { wrapper: wrap() })
    await waitFor(() => expect(result.current.data?.action.type).toBe('classify'))
    expect(result.current.data?.counts.openUnclassifiedCount).toBe(30)
  })

  it('useTriage returns pending proposals', async () => {
    const { result } = renderHook(() => useTriage(), { wrapper: wrap() })
    await waitFor(() => expect(result.current.data?.length).toBe(1))
    expect(result.current.data?.[0].proposedFeatureSlug).toBe('sessions')
  })

  it('useAdvanceMap posts advance-map and refreshes the wizard', async () => {
    const pm = await import('@/api/pm')
    vi.mocked(pm.advanceMap).mockResolvedValueOnce({ onboardingState: 'map_merged' })
    const { useAdvanceMap } = await import('@/api/hooks')
    const { result } = renderHook(() => useAdvanceMap(), { wrapper: wrap() })
    result.current.mutate('ccswitch')
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(pm.advanceMap).toHaveBeenCalledWith('ccswitch')
  })

  it('useAdvanceLive posts advance-live', async () => {
    const pm = await import('@/api/pm')
    vi.mocked(pm.advanceLive).mockResolvedValueOnce({ onboardingState: 'live' })
    const { useAdvanceLive } = await import('@/api/hooks')
    const { result } = renderHook(() => useAdvanceLive(), { wrapper: wrap() })
    result.current.mutate('ccswitch')
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(pm.advanceLive).toHaveBeenCalledWith('ccswitch')
  })

  it('useSyncProject toasts on success — a 202 with no feedback read as "nothing happened"', async () => {
    const pm = await import('@/api/pm')
    vi.mocked(pm.requestProjectSync).mockResolvedValueOnce({ requested: true })
    const { toast } = await import('sonner')
    const { useSyncProject } = await import('@/api/hooks')
    const { result } = renderHook(() => useSyncProject(), { wrapper: wrap() })
    result.current.mutate('ccswitch')
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(toast.success).toHaveBeenCalledWith('Sync requested for ccswitch')
  })
})
