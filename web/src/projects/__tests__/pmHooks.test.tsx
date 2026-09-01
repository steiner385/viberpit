import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { useOnboarding, useTriage } from '@/api/hooks'

vi.mock('@/api/pm', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/pm')>()),
  fetchOnboarding: vi.fn().mockResolvedValue({
    state: 'map_merged',
    action: { type: 'classify' },
    counts: { repoCount: 1, backfillDoneCount: 1, featureCount: 4,
              openUnclassifiedCount: 30, pendingProposalCount: 0 },
  }),
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
})
