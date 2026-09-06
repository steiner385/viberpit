/**
 * Triage inbox hooks (PM product layer, phase 3).
 *
 * The inbox is keyboard-first: a row leaves the list the instant you press `a`,
 * so the ONLY thing that tells you an accept failed is the row coming back plus
 * a toast. These tests pin exactly that — optimistic removal, rollback on error,
 * and the single toast (the hooks keep `suppressErrorToast` meta so the global
 * MutationCache handler does not say it a second time).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

vi.mock('@/ws/client', () => ({ useSessionSocket: () => ({ on: vi.fn(), off: vi.fn() }) }))
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }))
vi.mock('@/api/triage', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/triage')>()
  return {
    ...actual,
    fetchTriage: vi.fn(),
    acceptProposal: vi.fn(),
    rejectProposal: vi.fn(),
    acceptBulk: vi.fn(),
    updateOverlay: vi.fn(),
  }
})

import { toast } from 'sonner'
import { QK, useAcceptProposal, useAcceptBulk, useUpdateOverlay } from '@/api/hooks'
import * as triage from '@/api/triage'

const mocked = vi.mocked(triage)

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

function seedRows(qc: QueryClient) {
  const seed = [
    { id: 'p1', workItemId: 'w1', title: 'A' },
    { id: 'p2', workItemId: 'w2', title: 'B' },
  ] as unknown as triage.TriageProposal[]
  qc.setQueryData([...QK.triage, null], seed)
  return seed
}

function rowIds(qc: QueryClient) {
  return qc
    .getQueryData<triage.TriageProposal[]>([...QK.triage, null])
    ?.map((r) => r.id)
}

beforeEach(() => vi.clearAllMocks())

describe('useAcceptProposal optimistic rollback', () => {
  it('removes the row optimistically and restores it when accept fails', async () => {
    const qc = makeClient()
    seedRows(qc)
    let release: (() => void) | undefined
    mocked.acceptProposal.mockImplementationOnce(
      () => new Promise((_res, rej) => { release = () => rej(new Error('boom')) }),
    )

    const { result } = renderHook(() => useAcceptProposal(), { wrapper: makeWrapper(qc) })
    act(() => result.current.mutate({ id: 'p1' }))

    // While the request is in flight the row is already gone.
    await waitFor(() => expect(rowIds(qc)).toEqual(['p2']))

    await act(async () => { release?.() })

    // Once the rejection settles, the optimistic removal is rolled back.
    await waitFor(() => expect(rowIds(qc)).toEqual(['p1', 'p2']))
    expect(toast.error).toHaveBeenCalledTimes(1)
  })

  it('leaves the row removed when accept succeeds', async () => {
    const qc = makeClient()
    seedRows(qc)
    mocked.acceptProposal.mockResolvedValueOnce({
      id: 'p1', status: 'accepted', workItemId: 'w1', triageState: 'accepted',
    })

    const { result } = renderHook(() => useAcceptProposal(), { wrapper: makeWrapper(qc) })
    await act(async () => { result.current.mutate({ id: 'p1' }) })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(rowIds(qc)).toEqual(['p2'])
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('forwards edits to the fetcher', async () => {
    const qc = makeClient()
    seedRows(qc)
    mocked.acceptProposal.mockResolvedValueOnce({
      id: 'p1', status: 'accepted', workItemId: 'w1', triageState: 'accepted',
    })

    const { result } = renderHook(() => useAcceptProposal(), { wrapper: makeWrapper(qc) })
    await act(async () => { result.current.mutate({ id: 'p1', edits: { reach: 4 } }) })

    await waitFor(() =>
      expect(mocked.acceptProposal).toHaveBeenCalledWith('p1', { reach: 4 }))
  })
})

describe('useAcceptBulk', () => {
  it('drops every selected row and rolls all of them back on failure', async () => {
    const qc = makeClient()
    seedRows(qc)
    let release: (() => void) | undefined
    mocked.acceptBulk.mockImplementationOnce(
      () => new Promise((_res, rej) => { release = () => rej(new Error('boom')) }),
    )

    const { result } = renderHook(() => useAcceptBulk(), { wrapper: makeWrapper(qc) })
    act(() => result.current.mutate(['p1', 'p2']))

    await waitFor(() => expect(rowIds(qc)).toEqual([]))
    await act(async () => { release?.() })
    await waitFor(() => expect(rowIds(qc)).toEqual(['p1', 'p2']))
    expect(toast.error).toHaveBeenCalledTimes(1)
  })

  it('reports how many were accepted', async () => {
    const qc = makeClient()
    seedRows(qc)
    mocked.acceptBulk.mockResolvedValueOnce({
      accepted: 2, results: [{ id: 'p1', ok: true }, { id: 'p2', ok: true }],
    })

    const { result } = renderHook(() => useAcceptBulk(), { wrapper: makeWrapper(qc) })
    await act(async () => { result.current.mutate(['p1', 'p2']) })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(mocked.acceptBulk).toHaveBeenCalledWith(['p1', 'p2'])
    expect(toast.success).toHaveBeenCalledWith('Accepted 2')
    expect(rowIds(qc)).toEqual([])
  })
})

describe('useUpdateOverlay', () => {
  it('PATCHes the overlay and toasts on failure', async () => {
    const qc = makeClient()
    mocked.updateOverlay.mockRejectedValueOnce(new Error('boom'))

    const { result } = renderHook(() => useUpdateOverlay(), { wrapper: makeWrapper(qc) })
    await act(async () => {
      result.current.mutate({ itemId: 'w1', edit: { triageState: 'accepted' } })
    })

    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(mocked.updateOverlay).toHaveBeenCalledWith('w1', { triageState: 'accepted' })
    expect(toast.error).toHaveBeenCalledTimes(1)
  })
})
