import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { ApiRequestError, api } from '@/api/client'
import { QK, useUpdateProductPolicy } from '@/api/hooks'
import type { Project } from '@/api/pm'

vi.mock('@/ws/client', () => ({ useSessionSocket: () => ({ on: vi.fn(), off: vi.fn() }) }))
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }))

beforeEach(() => vi.restoreAllMocks())

function setup() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: 3 } } })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  const hook = renderHook(() => useUpdateProductPolicy('owner/product'), { wrapper })
  return { qc, ...hook }
}

describe('product policy edits', () => {
  it('sends sparse choices and the exact revision, then installs the merged response', async () => {
    const patch = { policy: { weight: 4 }, expectedUpdatedAt: '2026-09-17T01:00:00.123456+00:00' }
    const project = { automationPolicy: { dispatch: 'off', weight: 4 }, automationPolicyUpdatedAt: 'next' } as unknown as Project
    const send = vi.spyOn(api, 'patch').mockResolvedValue(project)
    const { qc, result } = setup()
    await act(async () => { await result.current.mutateAsync(patch) })
    expect(send).toHaveBeenCalledWith('/pm/projects/owner%2Fproduct/policy', patch)
    expect(qc.getQueryData(QK.project('owner/product'))).toEqual(project)
  })

  it('refreshes stale project data without retrying or overwriting it', async () => {
    const send = vi.spyOn(api, 'patch').mockRejectedValue(new ApiRequestError(409, { detail: { message: 'Settings changed' } }))
    const { qc, result } = setup()
    qc.setQueryData(QK.project('owner/product'), { automationPolicy: { dispatch: 'off' } })
    const refresh = vi.spyOn(qc, 'invalidateQueries')
    await act(async () => {
      await expect(result.current.mutateAsync({ policy: { dispatch: 'auto' }, expectedUpdatedAt: null })).rejects.toBeInstanceOf(ApiRequestError)
    })
    expect(send).toHaveBeenCalledTimes(1)
    expect(refresh).toHaveBeenCalledWith({ queryKey: QK.project('owner/product') })
    expect(qc.getQueryData(QK.project('owner/product'))).toEqual({ automationPolicy: { dispatch: 'off' } })
    await waitFor(() => expect(result.current.isError).toBe(true))
  })

  it('stays pending through the conflict refresh', async () => {
    vi.spyOn(api, 'patch').mockRejectedValue(new ApiRequestError(409, {}))
    const { qc, result } = setup()
    let finish: () => void = () => {}
    const refresh = vi.spyOn(qc, 'invalidateQueries').mockImplementation(() => new Promise<void>((resolve) => { finish = resolve }))
    act(() => { result.current.mutate({ policy: { weight: 3 }, expectedUpdatedAt: null }) })
    await waitFor(() => expect(refresh).toHaveBeenCalled())
    expect(result.current.isPending).toBe(true)
    await act(async () => { finish() })
    await waitFor(() => expect(result.current.isPending).toBe(false))
  })
})
