/**
 * Product dependency edges (roadmap spec §8 "Product dependency").
 *
 * Removing an edge is a chip's `X` — a direct, non-modal affordance with no
 * loading state of its own — so the ONLY thing that tells you it failed is the
 * chip coming back plus a toast. These pin exactly that, in the triage hooks'
 * shape: the project cache loses the edge on click and regains it on error.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

vi.mock('@/ws/client', () => ({ useSessionSocket: () => ({ on: vi.fn(), off: vi.fn() }) }))
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }))
vi.mock('@/api/pm', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/pm')>()
  return { ...actual, deleteEdge: vi.fn(), createEdge: vi.fn() }
})

import { toast } from 'sonner'
import { QK, useDeleteEdge } from '@/api/hooks'
import type { Project } from '@/api/pm'
import * as pm from '@/api/pm'

const mocked = vi.mocked(pm)

function makeWrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
}

function makeClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
}

function seedProject(qc: QueryClient) {
  qc.setQueryData(QK.project('kindash'), {
    id: 'p1', slug: 'kindash', name: 'KinDash', description: null, status: null, portfolioTags: [],
    onboardingState: 'live', createdAt: null, updatedAt: null, repos: [], featureCount: 0, workItemCount: 0,
    edges: [
      { id: 'e1', srcSlug: 'kindash', dstSlug: 'web', kind: 'depends_on', note: null },
      { id: 'e2', srcSlug: 'kindash', dstSlug: 'infra', kind: 'deploys_via', note: null },
    ],
  } as unknown as Project)
}

const edgeIds = (qc: QueryClient) =>
  qc.getQueryData<Project>(QK.project('kindash'))?.edges.map((e) => e.id)

beforeEach(() => vi.clearAllMocks())

describe('useDeleteEdge', () => {
  it('drops the chip from the project cache immediately and restores it when the request fails', async () => {
    const qc = makeClient()
    seedProject(qc)
    let release: (() => void) | undefined
    mocked.deleteEdge.mockImplementationOnce(
      () => new Promise((_res, rej) => { release = () => rej(new Error('boom')) }),
    )

    const { result } = renderHook(() => useDeleteEdge('kindash'), { wrapper: makeWrapper(qc) })
    act(() => result.current.mutate('e1'))

    await waitFor(() => expect(edgeIds(qc)).toEqual(['e2']))

    await act(async () => { release?.() })

    await waitFor(() => expect(edgeIds(qc)).toEqual(['e1', 'e2']))
    expect(toast.error).toHaveBeenCalledTimes(1)
  })

  it('leaves the chip gone when the delete succeeds', async () => {
    const qc = makeClient()
    seedProject(qc)
    mocked.deleteEdge.mockResolvedValueOnce(undefined)

    const { result } = renderHook(() => useDeleteEdge('kindash'), { wrapper: makeWrapper(qc) })
    await act(async () => { result.current.mutate('e1') })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(mocked.deleteEdge).toHaveBeenCalledWith('kindash', 'e1')
    expect(edgeIds(qc)).toEqual(['e2'])
    expect(toast.error).not.toHaveBeenCalled()
  })
})
