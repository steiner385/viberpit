import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useMarkSessionViewed, useMarkSessionUnread, QK } from './hooks'
import { api } from './client'
import type { ArchivedSession, Session } from './types'

vi.mock('./client', () => ({ api: { post: vi.fn() }, ApiRequestError: class extends Error {} }))

const live = (id: string, unread: boolean): Session => ({
  id, name: id, accountName: 'a', engine: 'claude_code', engineSessionId: null,
  status: 'idle', createdAt: 'x', prs: [], unread,
})
const archived = (id: string, unread: boolean): ArchivedSession => ({
  id, name: id, account: 'a', cwd: '/', repo: null, engine: 'claude_code', engineSessionId: null,
  permissionMode: 'default', createdAt: 'x', archivedAt: 'y', unread,
})

describe('read-state mutations (D-10)', () => {
  let qc: QueryClient
  beforeEach(() => {
    vi.clearAllMocks()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>

  it('useMarkSessionViewed posts /viewed, clears unread in both caches, then invalidates them', async () => {
    qc.setQueryData(QK.sessions, [live('s1', true), live('s2', true)])
    qc.setQueryData(QK.archived, [archived('s1', true)])
    let resolve!: () => void
    vi.mocked(api.post).mockReturnValue(new Promise<void>((r) => { resolve = r }) as never)
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    const { result } = renderHook(() => useMarkSessionViewed(), { wrapper })
    result.current.mutate('s1')
    // Optimistic: flipped before the server answers.
    await waitFor(() => {
      expect(qc.getQueryData<Session[]>(QK.sessions)?.map((s) => s.unread)).toEqual([false, true])
      expect(qc.getQueryData<ArchivedSession[]>(QK.archived)?.[0].unread).toBe(false)
    })
    expect(api.post).toHaveBeenCalledWith('/sessions/s1/viewed')
    expect(invalidate).not.toHaveBeenCalled()
    resolve()
    await waitFor(() => {
      expect(invalidate).toHaveBeenCalledWith({ queryKey: QK.sessions })
      expect(invalidate).toHaveBeenCalledWith({ queryKey: QK.archived })
    })
  })

  it('useMarkSessionUnread posts /unread and sets unread in both caches', async () => {
    qc.setQueryData(QK.sessions, [live('s1', false)])
    qc.setQueryData(QK.archived, [archived('s1', false)])
    vi.mocked(api.post).mockResolvedValue(undefined as never)
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    const { result } = renderHook(() => useMarkSessionUnread(), { wrapper })
    result.current.mutate('s1')
    await waitFor(() => {
      expect(qc.getQueryData<Session[]>(QK.sessions)?.[0].unread).toBe(true)
      expect(qc.getQueryData<ArchivedSession[]>(QK.archived)?.[0].unread).toBe(true)
    })
    expect(api.post).toHaveBeenCalledWith('/sessions/s1/unread')
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: QK.sessions }))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: QK.archived })
  })

  it('skips the refetch when the optimistic flip changed no cached row', async () => {
    qc.setQueryData(QK.sessions, [live('s1', false), { ...live('s2', false), unread: undefined }])
    qc.setQueryData(QK.archived, [archived('s1', false)])
    vi.mocked(api.post).mockResolvedValue(undefined as never)
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    const { result } = renderHook(() => useMarkSessionViewed(), { wrapper })
    result.current.mutate('s1')
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    result.current.mutate('s2') // absent flag = already read
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(invalidate).not.toHaveBeenCalled()
    // An absent flag stays absent — it is not rewritten to `false`.
    expect(qc.getQueryData<Session[]>(QK.sessions)?.[1]).not.toHaveProperty('unread', false)
  })

  it('never cancels a list fetch it has no row to flip in — a cancelled fetch nobody refetches is lost', async () => {
    // The archived Reader posts viewed on mount while `/sessions/archived` is
    // still loading: cancelling that fetch without a follow-up invalidate would
    // leave the list (and the page title) unloaded.
    qc.setQueryData(QK.sessions, [live('s1', true)])
    vi.mocked(api.post).mockResolvedValue(undefined as never)
    const cancel = vi.spyOn(qc, 'cancelQueries')
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    const { result } = renderHook(() => useMarkSessionViewed({ auto: true }), { wrapper })
    result.current.mutate('arc-1') // in neither cache
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(cancel).not.toHaveBeenCalled()
    expect(invalidate).not.toHaveBeenCalled()
    result.current.mutate('s1') // a live row only: the archived list is left alone
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: QK.sessions }))
    expect(cancel).toHaveBeenCalledWith({ queryKey: QK.sessions })
    expect(cancel).not.toHaveBeenCalledWith({ queryKey: QK.archived })
  })

  it('a manual "Mark as read" on an archived-only row refetches the archived list, not the live one', async () => {
    qc.setQueryData(QK.sessions, [live('s2', false)])
    qc.setQueryData(QK.archived, [archived('a1', true)])
    vi.mocked(api.post).mockResolvedValue(undefined as never)
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    const { result } = renderHook(() => useMarkSessionViewed(), { wrapper })
    result.current.mutate('a1')
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: QK.archived }))
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: QK.sessions })
  })

  it('skips the refetch for "Mark as unread" on an already-unread row', async () => {
    qc.setQueryData(QK.sessions, [live('s1', true)])
    vi.mocked(api.post).mockResolvedValue(undefined as never)
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    const { result } = renderHook(() => useMarkSessionUnread(), { wrapper })
    result.current.mutate('s1')
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(invalidate).not.toHaveBeenCalled()
  })

  it('the auto path refetches the live list only, never the archived list', async () => {
    qc.setQueryData(QK.sessions, [live('s1', true)])
    qc.setQueryData(QK.archived, [archived('s1', true)])
    vi.mocked(api.post).mockResolvedValue(undefined as never)
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    const { result } = renderHook(() => useMarkSessionViewed({ auto: true }), { wrapper })
    result.current.mutate('s1')
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: QK.sessions }))
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: QK.archived })
    expect(qc.getQueryData<ArchivedSession[]>(QK.archived)?.[0].unread).toBe(false)
  })

  it('leaves empty caches empty', async () => {
    vi.mocked(api.post).mockResolvedValue(undefined as never)
    const { result } = renderHook(() => useMarkSessionViewed(), { wrapper })
    result.current.mutate('s1')
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(qc.getQueryData(QK.sessions)).toBeUndefined()
    expect(qc.getQueryData(QK.archived)).toBeUndefined()
  })
})
