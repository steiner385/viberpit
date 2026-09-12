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
    undoProposal: vi.fn(),
  }
})

import { toast } from 'sonner'
import { QK, useAcceptProposal, useAcceptBulk, useUpdateOverlay, useUndoProposal } from '@/api/hooks'
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
    expect(toast.success).toHaveBeenCalledWith('Accepted 2', expect.objectContaining({ duration: 6000 }))
    expect(rowIds(qc)).toEqual([])
  })
})

describe('useUpdateOverlay', () => {
  it('PATCHes the overlay and toasts on failure', async () => {
    const qc = makeClient()
    mocked.updateOverlay.mockRejectedValueOnce(new Error('boom'))

    const { result } = renderHook(() => useUpdateOverlay(), { wrapper: makeWrapper(qc) })
    // A REAL triage state (`unclassified|proposed|classified|needs_human`): the
    // fetcher is mocked here, so a made-up one would pin a body the endpoint
    // 422s and the test would still be green.
    await act(async () => {
      result.current.mutate({ itemId: 'w1', edit: { triageState: 'classified' } })
    })

    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(mocked.updateOverlay).toHaveBeenCalledWith('w1', { triageState: 'classified' })
    expect(toast.error).toHaveBeenCalledTimes(1)
  })

  it('invalidates triage, every leaves list and the map on success — a score reorders leaves lists, a capability change moves counts on the map', async () => {
    const qc = makeClient()
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries')
    mocked.updateOverlay.mockResolvedValueOnce({
      id: 'w1', repo: 'o/r', number: 7, kind: 'issue', title: 'A', state: 'open',
      featureId: 'f2', reach: 5, impact: 5, effort: 4, confidence: 1,
      triageState: 'classified', classifiedBy: null, classifiedAt: null, url: null,
    })

    const { result } = renderHook(() => useUpdateOverlay(), { wrapper: makeWrapper(qc) })
    await act(async () => {
      result.current.mutate({ itemId: 'w1', edit: { effort: 4 } })
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QK.triage })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['pm', 'leaves'] })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QK.roadmapMap })
  })
})

describe('undo', () => {
  it('accept shows a 6 s Undo toast whose action calls the undo endpoint and refreshes', async () => {
    const qc = makeClient()
    seedRows(qc)
    mocked.acceptProposal.mockResolvedValueOnce({
      id: 'p1', status: 'accepted', workItemId: 'w1', triageState: 'accepted',
    })

    const { result } = renderHook(() => useAcceptProposal(), { wrapper: makeWrapper(qc) })
    await act(async () => { await result.current.mutateAsync({ id: 'p1' }) })

    expect(toast.success).toHaveBeenCalledWith(
      'Accepted',
      expect.objectContaining({ duration: 6000, action: expect.objectContaining({ label: 'Undo' }) }),
    )

    mocked.undoProposal.mockResolvedValueOnce({ id: 'p1', status: 'pending', workItemId: 'w1', triageState: 'proposed' })
    const call = vi.mocked(toast.success).mock.calls.at(-1)![1] as unknown as { action: { onClick: () => void } }
    await act(async () => { call.action.onClick() })
    expect(mocked.undoProposal).toHaveBeenCalledWith('p1')
  })

  it('bulk accept undoes every landed id from one toast', async () => {
    const qc = makeClient()
    seedRows(qc)
    mocked.acceptBulk.mockResolvedValueOnce({
      accepted: 2,
      results: [{ id: 'p1', ok: true }, { id: 'p2', ok: true }, { id: 'c', ok: false, error: 'not_pending' }],
    })

    const { result } = renderHook(() => useAcceptBulk(), { wrapper: makeWrapper(qc) })
    await act(async () => { await result.current.mutateAsync(['p1', 'p2', 'c']) })

    const call = vi.mocked(toast.success).mock.calls.at(-1)!
    expect(call[0]).toBe('Accepted 2')

    mocked.undoProposal.mockResolvedValue({ id: 'x', status: 'pending', workItemId: null, triageState: 'proposed' })
    await act(async () => { (call[1] as unknown as { action: { onClick: () => void } }).action.onClick() })
    expect(mocked.undoProposal.mock.calls.map(([id]) => id)).toEqual(['p1', 'p2'])
  })

  it('useUndoProposal posts and toasts on failure', async () => {
    const qc = makeClient()
    mocked.undoProposal.mockRejectedValueOnce(new Error('boom'))

    const { result } = renderHook(() => useUndoProposal(), { wrapper: makeWrapper(qc) })
    await act(async () => { await result.current.mutateAsync('p1').catch(() => undefined) })

    expect(mocked.undoProposal).toHaveBeenCalledWith('p1')
    expect(toast.error).toHaveBeenCalled()
  })
})
