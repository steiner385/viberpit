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
    rejectBulk: vi.fn(),
    dismissProposal: vi.fn(),
    dismissBulk: vi.fn(),
    updateOverlay: vi.fn(),
    undoProposal: vi.fn(),
    fetchDecisions: vi.fn(),
    acceptDecision: vi.fn(),
    rejectDecision: vi.fn(),
  }
})

import { toast } from 'sonner'
import {
  QK, useAcceptProposal, useAcceptBulk, useRejectBulk, useAcceptDecision, useRejectDecision,
  useUpdateOverlay, useUndoProposal, useDismissProposal, useDismissBulk, useRejectProposal,
} from '@/api/hooks'
import { ApiRequestError } from '@/api/client'
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

describe('useRejectBulk', () => {
  it('drops every selected row and rolls all of them back on failure', async () => {
    const qc = makeClient()
    seedRows(qc)
    let release: (() => void) | undefined
    mocked.rejectBulk.mockImplementationOnce(
      () => new Promise((_res, rej) => { release = () => rej(new Error('boom')) }),
    )

    const { result } = renderHook(() => useRejectBulk(), { wrapper: makeWrapper(qc) })
    act(() => result.current.mutate(['p1', 'p2']))

    await waitFor(() => expect(rowIds(qc)).toEqual([]))
    await act(async () => { release?.() })
    await waitFor(() => expect(rowIds(qc)).toEqual(['p1', 'p2']))
    expect(toast.error).toHaveBeenCalledTimes(1)
  })

  it('reports how many were rejected, with an undo that covers only the landed ids', async () => {
    const qc = makeClient()
    seedRows(qc)
    mocked.rejectBulk.mockResolvedValueOnce({
      rejected: 1, results: [{ id: 'p1', ok: true }, { id: 'p2', ok: false, error: 'not_pending' }],
    })
    mocked.undoProposal.mockResolvedValue({ id: 'p1', status: 'pending', workItemId: 'w1', triageState: 'proposed' })

    const { result } = renderHook(() => useRejectBulk(), { wrapper: makeWrapper(qc) })
    await act(async () => { result.current.mutate(['p1', 'p2']) })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(mocked.rejectBulk).toHaveBeenCalledWith(['p1', 'p2'])
    expect(toast.success).toHaveBeenCalledWith('Rejected 1', expect.objectContaining({ duration: 6000 }))
    // The toast's Undo puts back what landed — not the id the server refused.
    const call = vi.mocked(toast.success).mock.calls.at(-1)!
    await act(async () => { (call[1] as unknown as { action: { onClick: () => void } }).action.onClick() })
    await waitFor(() => expect(mocked.undoProposal).toHaveBeenCalledTimes(1))
    expect(mocked.undoProposal).toHaveBeenCalledWith('p1')
  })

  it('says nothing when the sweep landed nothing', async () => {
    const qc = makeClient()
    seedRows(qc)
    mocked.rejectBulk.mockResolvedValueOnce({
      rejected: 0, results: [{ id: 'p1', ok: false, error: 'not_pending' }],
    })
    const { result } = renderHook(() => useRejectBulk(), { wrapper: makeWrapper(qc) })
    await act(async () => { result.current.mutate(['p1']) })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(toast.success).not.toHaveBeenCalled()
  })
})

describe('useDismissProposal', () => {
  it('removes the row now, and on a server without the route (404) restores it with one toast', async () => {
    const qc = makeClient()
    seedRows(qc)
    let release: (() => void) | undefined
    mocked.dismissProposal.mockImplementationOnce(
      () => new Promise((_res, rej) => { release = () => rej(new ApiRequestError(404, { detail: 'Not Found' })) }),
    )
    const { result } = renderHook(() => useDismissProposal(), { wrapper: makeWrapper(qc) })
    act(() => result.current.mutate({ id: 'p1' }))
    await waitFor(() => expect(rowIds(qc)).toEqual(['p2']))
    await act(async () => { release?.() })
    await waitFor(() => expect(rowIds(qc)).toEqual(['p1', 'p2']))
    expect(toast.error).toHaveBeenCalledTimes(1)
    expect(toast.success).not.toHaveBeenCalled()
  })

  it('a landed dismiss offers the same 6 s Undo through the one undo endpoint', async () => {
    const qc = makeClient()
    seedRows(qc)
    mocked.dismissProposal.mockResolvedValueOnce({ id: 'p1', status: 'dismissed', workItemId: 'w1' })
    const { result } = renderHook(() => useDismissProposal(), { wrapper: makeWrapper(qc) })
    await act(async () => { await result.current.mutateAsync({ id: 'p1' }) })
    expect(mocked.dismissProposal).toHaveBeenCalledWith('p1')
    expect(mocked.rejectProposal).not.toHaveBeenCalled()
    expect(rowIds(qc)).toEqual(['p2'])
    expect(toast.success).toHaveBeenCalledWith(
      'Dismissed', expect.objectContaining({ duration: 6000, action: expect.objectContaining({ label: 'Undo' }) }),
    )
    mocked.undoProposal.mockResolvedValueOnce({ id: 'p1', status: 'pending', workItemId: 'w1', triageState: 'proposed' })
    const call = vi.mocked(toast.success).mock.calls.at(-1)![1] as unknown as { action: { onClick: () => void } }
    await act(async () => { call.action.onClick() })
    expect(mocked.undoProposal).toHaveBeenCalledWith('p1')
  })

  it('reject keeps its own endpoint and its own toast', async () => {
    const qc = makeClient()
    seedRows(qc)
    mocked.rejectProposal.mockResolvedValueOnce({ id: 'p1', status: 'rejected' })
    const { result } = renderHook(() => useRejectProposal(), { wrapper: makeWrapper(qc) })
    await act(async () => { await result.current.mutateAsync({ id: 'p1' }) })
    expect(mocked.rejectProposal).toHaveBeenCalledWith('p1')
    expect(mocked.dismissProposal).not.toHaveBeenCalled()
    expect(toast.success).toHaveBeenCalledWith('Rejected', expect.anything())
  })
})

describe('useDismissBulk', () => {
  it('drops every selected row and rolls all of them back on failure', async () => {
    const qc = makeClient()
    seedRows(qc)
    let release: (() => void) | undefined
    mocked.dismissBulk.mockImplementationOnce(
      () => new Promise((_res, rej) => { release = () => rej(new ApiRequestError(405, { detail: 'Method Not Allowed' })) }),
    )
    const { result } = renderHook(() => useDismissBulk(), { wrapper: makeWrapper(qc) })
    act(() => result.current.mutate(['p1', 'p2']))
    await waitFor(() => expect(rowIds(qc)).toEqual([]))
    await act(async () => { release?.() })
    await waitFor(() => expect(rowIds(qc)).toEqual(['p1', 'p2']))
    expect(toast.error).toHaveBeenCalledTimes(1)
  })

  it('reports how many were dismissed, with an undo over only the landed ids', async () => {
    const qc = makeClient()
    seedRows(qc)
    mocked.dismissBulk.mockResolvedValueOnce({
      dismissed: 1, results: [{ id: 'p1', ok: true }, { id: 'p2', ok: false, error: 'not_pending' }],
    })
    mocked.undoProposal.mockResolvedValue({ id: 'p1', status: 'pending', workItemId: 'w1', triageState: 'proposed' })
    const { result } = renderHook(() => useDismissBulk(), { wrapper: makeWrapper(qc) })
    await act(async () => { result.current.mutate(['p1', 'p2']) })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(mocked.dismissBulk).toHaveBeenCalledWith(['p1', 'p2'])
    expect(toast.success).toHaveBeenCalledWith('Dismissed 1', expect.objectContaining({ duration: 6000 }))
    const call = vi.mocked(toast.success).mock.calls.at(-1)!
    await act(async () => { (call[1] as unknown as { action: { onClick: () => void } }).action.onClick() })
    await waitFor(() => expect(mocked.undoProposal).toHaveBeenCalledTimes(1))
    expect(mocked.undoProposal).toHaveBeenCalledWith('p1')
  })

  it('says nothing when the sweep landed nothing', async () => {
    const qc = makeClient()
    seedRows(qc)
    mocked.dismissBulk.mockResolvedValueOnce({ dismissed: 0, results: [{ id: 'p1', ok: false, error: 'not_pending' }] })
    const { result } = renderHook(() => useDismissBulk(), { wrapper: makeWrapper(qc) })
    await act(async () => { result.current.mutate(['p1']) })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(toast.success).not.toHaveBeenCalled()
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

/**
 * The inbox's second card kind (spec 2026-09-18 §6). Same optimistic shape as
 * the classification twins above — and deliberately NO undo toast: a decision
 * has no server-side undo window, so an Undo action here would be a lie.
 */
describe('decision proposals', () => {
  function seedDecisions(qc: QueryClient) {
    qc.setQueryData([...QK.decisions, null, false], [
      { id: 'd1', title: 'Sync or async?' },
      { id: 'd2', title: 'Which store?' },
    ] as unknown as triage.DecisionProposal[])
  }
  const decisionIds = (qc: QueryClient) =>
    qc.getQueryData<triage.DecisionProposal[]>([...QK.decisions, null, false])?.map((r) => r.id)

  it('accepting a decision removes it from every cached decisions list (scoped and unscoped)', async () => {
    const qc = makeClient()
    seedDecisions(qc)
    qc.setQueryData([...QK.decisions, 'kindash', true], [{ id: 'd1' }] as unknown as triage.DecisionProposal[])
    mocked.acceptDecision.mockResolvedValueOnce({ id: 'd1', status: 'accepted', chosenKey: 'a' })
    const { result } = renderHook(() => useAcceptDecision(), { wrapper: makeWrapper(qc) })
    await act(async () => { await result.current.mutateAsync({ id: 'd1', chosenKey: 'a' }) })
    expect(decisionIds(qc)).toEqual(['d2'])
    expect(qc.getQueryData([...QK.decisions, 'kindash', true])).toHaveLength(0)
  })

  it('accept sends the chosen key, drops the row, and does not offer an undo', async () => {
    const qc = makeClient()
    seedDecisions(qc)
    mocked.acceptDecision.mockResolvedValueOnce({ id: 'd1', status: 'accepted', chosenKey: 'b' })

    const { result } = renderHook(() => useAcceptDecision(), { wrapper: makeWrapper(qc) })
    await act(async () => { await result.current.mutateAsync({ id: 'd1', chosenKey: 'b' }) })

    expect(mocked.acceptDecision).toHaveBeenCalledWith('d1', 'b')
    expect(decisionIds(qc)).toEqual(['d2'])
    // A plain confirmation, never the `{ action: { label: 'Undo' } }` shape the
    // triage accept above carries.
    expect(toast.success).toHaveBeenCalledWith('Decision recorded')
  })

  it('refetches the list (rollback) and toasts once when the accept fails', async () => {
    const qc = makeClient()
    seedDecisions(qc)
    mocked.acceptDecision.mockRejectedValueOnce(new Error('409'))
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries')

    const { result } = renderHook(() => useAcceptDecision(), { wrapper: makeWrapper(qc) })
    await act(async () => {
      await result.current.mutateAsync({ id: 'd1', chosenKey: 'b' }).catch(() => undefined)
    })

    // Rollback is a refetch, not a snapshot restore (the list lives under N keys).
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QK.decisions })
    expect(toast.error).toHaveBeenCalledTimes(1)
    // …and the global MutationCache toast stays silenced, so it is said ONCE.
    expect(qc.getMutationCache().getAll().at(-1)?.options.meta)
      .toEqual({ suppressErrorToast: true })
  })

  it('reject drops the row and refreshes the views a hold change moves', async () => {
    const qc = makeClient()
    seedDecisions(qc)
    mocked.rejectDecision.mockResolvedValueOnce({ id: 'd2', status: 'rejected', chosenKey: null })
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries')

    const { result } = renderHook(() => useRejectDecision(), { wrapper: makeWrapper(qc) })
    await act(async () => { await result.current.mutateAsync({ id: 'd2' }) })

    expect(mocked.rejectDecision).toHaveBeenCalledWith('d2')
    expect(decisionIds(qc)).toEqual(['d1'])
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QK.decisions })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QK.roadmapMap })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QK.queue })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['pm', 'issue'] })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['pm', 'leaves'] })
  })

  it('accept refreshes the leaf lists too — a leaf row renders the hold it just lifted', async () => {
    // `Leaf.holdKind` drives the hold chip and the needs-you tier, so the leaf
    // list a person may have open kept saying "Awaiting your decision" until
    // `useLeaves`' own 30 s poll fired. The two sibling hold-moving mutations
    // (`useReleaseHold`, `useDispositionIssue`) both invalidate this key.
    const qc = makeClient()
    seedDecisions(qc)
    mocked.acceptDecision.mockResolvedValueOnce({ id: 'd1', status: 'accepted', chosenKey: 'b' })
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries')

    const { result } = renderHook(() => useAcceptDecision(), { wrapper: makeWrapper(qc) })
    await act(async () => { await result.current.mutateAsync({ id: 'd1', chosenKey: 'b' }) })

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['pm', 'leaves'] })
  })
})
