/**
 * Fix round 1 / I3: every mutation that touches a field the capacity readout
 * itself carries (the governor's push level/mode, a product's rank, a
 * product's capacity_mode/max_in_flight) must invalidate `QK.capacity` on
 * success — otherwise the Capacity page can sit on a stale value until its
 * next 30s poll even though the write that changed it just succeeded.
 *
 * Modelled on `productPolicyHooks.test.tsx`: real hooks, a real `QueryClient`,
 * a mocked `api` transport, asserting via a spy on `qc.invalidateQueries`.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { api } from '@/api/client'
import {
  QK, useUpdateProductPolicy, useUpdateProject, usePatchAutomation, useReorderProject,
} from '@/api/hooks'

vi.mock('@/ws/client', () => ({ useSessionSocket: () => ({ on: vi.fn(), off: vi.fn() }) }))
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }))

beforeEach(() => vi.restoreAllMocks())

function setup() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  return { qc, wrapper }
}

describe('capacity invalidation (fix round 1 / I3)', () => {
  it('usePatchAutomation invalidates QK.capacity on success', async () => {
    vi.spyOn(api, 'patch').mockResolvedValue({ id: 'capacity-governor' })
    const { qc, wrapper } = setup()
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    const { result } = renderHook(() => usePatchAutomation(), { wrapper })
    await act(async () => {
      await result.current.mutateAsync({ id: 'capacity-governor', body: { config: { push_level: 'max' } } })
    })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: QK.capacity })
  })

  it('useUpdateProject invalidates QK.capacity on success', async () => {
    vi.spyOn(api, 'patch').mockResolvedValue({ slug: 'kindash' })
    const { qc, wrapper } = setup()
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    const { result } = renderHook(() => useUpdateProject('kindash'), { wrapper })
    await act(async () => {
      await result.current.mutateAsync({ priorityRank: 2 })
    })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: QK.capacity })
  })

  it('useUpdateProductPolicy invalidates QK.capacity on success', async () => {
    vi.spyOn(api, 'patch').mockResolvedValue({ slug: 'kindash' })
    const { qc, wrapper } = setup()
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    const { result } = renderHook(() => useUpdateProductPolicy('kindash'), { wrapper })
    await act(async () => {
      await result.current.mutateAsync({ policy: { capacity_mode: 'pinned' } })
    })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: QK.capacity })
  })

  it('useReorderProject invalidates QK.capacity (and QK.project/roadmapMap) on success', async () => {
    vi.spyOn(api, 'patch').mockResolvedValue({ slug: 'kindash' })
    const { qc, wrapper } = setup()
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    const { result } = renderHook(() => useReorderProject(), { wrapper })
    await act(async () => {
      await result.current.mutateAsync({ slug: 'kindash', priorityRank: 1 })
    })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: QK.capacity })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: QK.project('kindash') })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: QK.roadmapMap })
  })

  it('useReorderProject sends {slug, priorityRank} as a mutation VARIABLE — the same instance can target a different slug on the next call', async () => {
    const patch = vi.spyOn(api, 'patch').mockResolvedValue({ slug: 'x' })
    const { wrapper } = setup()
    const { result } = renderHook(() => useReorderProject(), { wrapper })
    await act(async () => { await result.current.mutateAsync({ slug: 'kindash', priorityRank: 1 }) })
    await act(async () => { await result.current.mutateAsync({ slug: 'viberpit', priorityRank: 2 }) })
    expect(patch).toHaveBeenNthCalledWith(1, '/pm/projects/kindash', { priorityRank: 1 })
    expect(patch).toHaveBeenNthCalledWith(2, '/pm/projects/viberpit', { priorityRank: 2 })
  })
})
