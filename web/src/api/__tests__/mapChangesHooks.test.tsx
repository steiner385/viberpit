/**
 * Map-changes fetchers + hooks (roadmap spec §8/§9; backend contract
 * `server/api/pm.py` `map_changes_edit`/`map_changes_abandon`/`map_changes_rebase`).
 * Pins the shapes 8c task 2/3's dialogs and task 4's PR card build on: one op
 * per request, both the map and the product cache invalidated, and R8c-5's copy
 * for the two GitHub-flavored error shapes (409 `conflict`, 502 `github`).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

vi.mock('@/ws/client', () => ({ useSessionSocket: () => ({ on: vi.fn(), off: vi.fn() }) }))
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }))
vi.mock('@/api/pm', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/pm')>()
  return { ...actual, editMapChanges: vi.fn(), abandonMapChanges: vi.fn(), rebaseMapChanges: vi.fn() }
})

import { toast } from 'sonner'
import { ApiRequestError } from '@/api/client'
import {
  QK, useEditMapChanges, useAbandonMapChanges, useRebaseMapChanges, mapChangesErrorMessage,
} from '@/api/hooks'
import type { MapChangesPointer } from '@/api/roadmap'
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

const pointer: MapChangesPointer = {
  branch: 'ccswitch/map-changes/kindash',
  prNumber: 42,
  prUrl: 'https://github.com/steiner385/kindash/pull/42',
  status: 'open',
  openedAt: '2026-09-12T00:00:00Z',
  headSha: 'abc123',
  behind: false,
  conflicting: false,
}

beforeEach(() => vi.clearAllMocks())

describe('useEditMapChanges', () => {
  it('sends the op to the right path and invalidates the map and the product', async () => {
    const qc = makeClient()
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries')
    mocked.editMapChanges.mockResolvedValueOnce({ pointer, path: 'docs/product/features/dictation.md', commitSha: 'sha1' })

    const { result } = renderHook(() => useEditMapChanges('kindash'), { wrapper: makeWrapper(qc) })
    await act(async () => {
      result.current.mutate({ op: 'rename', slug: 'dictation', title: 'X' })
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(mocked.editMapChanges).toHaveBeenCalledWith('kindash', { op: 'rename', slug: 'dictation', title: 'X' })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QK.roadmapMap })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QK.project('kindash') })
    expect(toast.error).not.toHaveBeenCalled()
  })

  // All three callers (both dialogs and the links editor) render the mapped
  // message inline in a `role="alert"` beside the form that failed — that is
  // exactly what `PM_MUTATION_META` ("the caller renders it") is for, and a
  // toast on top of it says the same thing twice. The copy itself is pinned on
  // `mapChangesErrorMessage` below. Abandon and Rebase DO toast: they are fired
  // from a banner with nowhere to put the message.
  it('does not toast a failure — the caller renders it inline', async () => {
    const qc = makeClient()
    mocked.editMapChanges.mockRejectedValueOnce(
      new ApiRequestError(409, { detail: { error: 'conflict', step: 'commit', message: 'sha mismatch' } }),
    )

    const { result } = renderHook(() => useEditMapChanges('kindash'), { wrapper: makeWrapper(qc) })
    await act(async () => {
      result.current.mutate({ op: 'rename', slug: 'dictation', title: 'X' })
    })

    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(toast.error).not.toHaveBeenCalled()
    expect(toast.success).not.toHaveBeenCalled()
  })
})

describe('useAbandonMapChanges', () => {
  it('DELETEs and invalidates the map and the product', async () => {
    const qc = makeClient()
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries')
    mocked.abandonMapChanges.mockResolvedValueOnce({
      pointer: { ...pointer, status: 'abandoned' },
      cleanup: { deleted: 2, orphaned: 1 },
    })

    const { result } = renderHook(() => useAbandonMapChanges('kindash'), { wrapper: makeWrapper(qc) })
    await act(async () => {
      result.current.mutate()
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(mocked.abandonMapChanges).toHaveBeenCalledWith('kindash')
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QK.roadmapMap })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QK.project('kindash') })
  })

  it('toasts a fallback message when the server gives none', async () => {
    const qc = makeClient()
    mocked.abandonMapChanges.mockRejectedValueOnce(new ApiRequestError(404, { detail: { error: 'no_open_map_changes' } }))

    const { result } = renderHook(() => useAbandonMapChanges('kindash'), { wrapper: makeWrapper(qc) })
    await act(async () => {
      result.current.mutate()
    })

    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(toast.error).toHaveBeenCalledWith('Could not abandon the map changes')
  })
})

describe('useRebaseMapChanges', () => {
  it('POSTs /rebase and invalidates the map and the product', async () => {
    const qc = makeClient()
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries')
    mocked.rebaseMapChanges.mockResolvedValueOnce({ pointer: { ...pointer, behind: false } })

    const { result } = renderHook(() => useRebaseMapChanges('kindash'), { wrapper: makeWrapper(qc) })
    await act(async () => {
      result.current.mutate()
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(mocked.rebaseMapChanges).toHaveBeenCalledWith('kindash')
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QK.roadmapMap })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: QK.project('kindash') })
  })

  it('toasts the not_behind message on a 409 that is not a conflict', async () => {
    const qc = makeClient()
    mocked.rebaseMapChanges.mockRejectedValueOnce(
      new ApiRequestError(409, { detail: { error: 'not_behind', message: 'PR is not behind base' } }),
    )

    const { result } = renderHook(() => useRebaseMapChanges('kindash'), { wrapper: makeWrapper(qc) })
    await act(async () => {
      result.current.mutate()
    })

    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(toast.error).toHaveBeenCalledWith('PR is not behind base')
  })
})

describe('mapChangesErrorMessage', () => {
  // R8c-5's two GitHub-flavored shapes. Pinned here rather than on the edit hook
  // (which no longer toasts): this mapper is where the copy lives, and it is
  // what all three callers render inline.
  it('gives a 409 conflict its own sentence', () => {
    const e = new ApiRequestError(409, { detail: { error: 'conflict', step: 'commit', message: 'sha mismatch' } })
    expect(mapChangesErrorMessage(e, 'fallback')).toBe('The map changed under you — try again.')
  })

  it('names the failing step on a 502 github error', () => {
    const e = new ApiRequestError(502, { detail: { error: 'github', step: 'create_pr', message: 'rate limited' } })
    expect(mapChangesErrorMessage(e, 'fallback')).toBe('GitHub refused the create_pr step: rate limited')
  })

  it('falls back for a non-ApiRequestError', () => {
    expect(mapChangesErrorMessage(new Error('boom'), 'fallback text')).toBe('fallback text')
  })

  it('reads the server message for a 422 validation error', () => {
    const e = new ApiRequestError(422, { detail: { error: 'bad_slug', message: 'slug is invalid' } })
    expect(mapChangesErrorMessage(e, 'fallback')).toBe('slug is invalid')
  })

  it('falls back when the body carries no message', () => {
    const e = new ApiRequestError(500, {})
    expect(mapChangesErrorMessage(e, 'fallback text')).toBe('fallback text')
  })
})
