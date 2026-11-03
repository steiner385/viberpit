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

  it('leaves empty caches empty', async () => {
    vi.mocked(api.post).mockResolvedValue(undefined as never)
    const { result } = renderHook(() => useMarkSessionViewed(), { wrapper })
    result.current.mutate('s1')
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(qc.getQueryData(QK.sessions)).toBeUndefined()
    expect(qc.getQueryData(QK.archived)).toBeUndefined()
  })
})
