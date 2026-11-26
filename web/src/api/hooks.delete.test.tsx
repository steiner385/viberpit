import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { usePermanentDeleteSession, useUnarchiveSession, QK } from './hooks'
import { api } from './client'
import type { ArchivedSession, Session } from './types'

vi.mock('./client', () => ({ api: { del: vi.fn(), post: vi.fn() }, ApiRequestError: class extends Error {} }))
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn(), info: vi.fn() } }))
import { toast } from 'sonner'

const arch: ArchivedSession = { id: 'a-1', name: 'Bar', account: 'a', cwd: '/x', repo: null, engine: 'claude_code', engineSessionId: null, permissionMode: 'interactive', createdAt: 'x', archivedAt: 'y' }

describe('usePermanentDeleteSession — optimistic', () => {
  let qc: QueryClient
  beforeEach(() => { vi.clearAllMocks(); qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }) })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>

  it('removes from archived immediately; the card leaving IS the acknowledgement (no toast)', async () => {
    qc.setQueryData(QK.archived, [arch])
    vi.mocked(api.del).mockResolvedValue(undefined)
    const { result } = renderHook(() => usePermanentDeleteSession(), { wrapper })
    result.current.mutate('a-1')
    await waitFor(() => expect(qc.getQueryData<ArchivedSession[]>(QK.archived)).toEqual([]))
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(toast.success).not.toHaveBeenCalled()
  })

  it('rolls back and error-toasts on failure', async () => {
    qc.setQueryData(QK.archived, [arch])
    vi.mocked(api.del).mockRejectedValue(new Error('no'))
    const { result } = renderHook(() => usePermanentDeleteSession(), { wrapper })
    result.current.mutate('a-1')
    await waitFor(() => {
      expect(qc.getQueryData<ArchivedSession[]>(QK.archived)).toEqual([arch])
      expect(toast.error).toHaveBeenCalledWith("Couldn't delete Bar")
    })
  })
})

describe('usePermanentDeleteSession — 202 keeps the row (FR-404)', () => {
  let qc: QueryClient
  beforeEach(() => { vi.clearAllMocks(); qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }) })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>

  it('restores the archived row with deleteState=pending and says the worktree is still being removed', async () => {
    qc.setQueryData(QK.archived, [arch])
    vi.mocked(api.del).mockResolvedValue({ state: 'pending' })
    const { result } = renderHook(() => usePermanentDeleteSession(), { wrapper })
    result.current.mutate('a-1')
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(qc.getQueryData<ArchivedSession[]>(QK.archived)).toEqual([{ ...arch, deleteState: 'pending' }])
    expect(toast.info).toHaveBeenCalledWith(expect.stringMatching(/Deleting…/))
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('restores the archived row with deleteState=stuck and says it needs manual cleanup', async () => {
    qc.setQueryData(QK.archived, [arch])
    vi.mocked(api.del).mockResolvedValue({ state: 'stuck' })
    const { result } = renderHook(() => usePermanentDeleteSession(), { wrapper })
    result.current.mutate('a-1')
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(qc.getQueryData<ArchivedSession[]>(QK.archived)).toEqual([{ ...arch, deleteState: 'stuck' }])
    expect(toast.info).toHaveBeenCalledWith(expect.stringMatching(/stuck.*manual cleanup/i))
  })

  it('restores a live row too when the server kept it', async () => {
    const liveSess: Session = { id: 'a-1', name: 'Bar', accountName: 'a', engine: 'claude_code', engineSessionId: null, status: 'idle', createdAt: 'x', updatedAt: 'x', prs: [] }
    qc.setQueryData(QK.sessions, [liveSess])
    vi.mocked(api.del).mockResolvedValue({ state: 'pending' })
    const { result } = renderHook(() => usePermanentDeleteSession(), { wrapper })
    result.current.mutate('a-1')
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(qc.getQueryData<Session[]>(QK.sessions)).toEqual([liveSess])
  })

  it('keeps the optimistic removal (and stays silent) on a plain 204', async () => {
    qc.setQueryData(QK.archived, [arch])
    vi.mocked(api.del).mockResolvedValue(undefined)
    const { result } = renderHook(() => usePermanentDeleteSession(), { wrapper })
    result.current.mutate('a-1')
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(qc.getQueryData<ArchivedSession[]>(QK.archived)).toEqual([])
    expect(toast.info).not.toHaveBeenCalled()
  })
})

describe('useUnarchiveSession — optimistic', () => {
  let qc: QueryClient
  beforeEach(() => { vi.clearAllMocks(); qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }) })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>

  it('removes from archived immediately on mutate', async () => {
    qc.setQueryData(QK.archived, [arch])
    vi.mocked(api.post).mockResolvedValue(arch)
    const { result } = renderHook(() => useUnarchiveSession(), { wrapper })
    result.current.mutate('a-1')
    await waitFor(() => expect(qc.getQueryData<ArchivedSession[]>(QK.archived)).toEqual([]))
  })

  it('rolls back the archived cache and fires an error toast on failure', async () => {
    qc.setQueryData(QK.archived, [arch])
    vi.mocked(api.post).mockRejectedValue(new Error('network error'))
    const { result } = renderHook(() => useUnarchiveSession(), { wrapper })
    result.current.mutate('a-1')
    await waitFor(() => {
      expect(qc.getQueryData<ArchivedSession[]>(QK.archived)).toEqual([arch])
      expect(toast.error).toHaveBeenCalledWith("Couldn't restore Bar")
    })
  })
})

describe('usePermanentDeleteSession — dual-cache rollback', () => {
  let qc: QueryClient
  beforeEach(() => { vi.clearAllMocks(); qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }) })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>

  it('rolls back both sessions and archived caches on failure', async () => {
    const liveSess: Session = { id: 'a-1', name: 'Bar', accountName: 'a', engine: 'claude_code', engineSessionId: null, status: 'idle', createdAt: 'x', updatedAt: 'x', prs: [] }
    qc.setQueryData(QK.archived, [arch])
    qc.setQueryData(QK.sessions, [liveSess])
    vi.mocked(api.del).mockRejectedValue(new Error('500'))
    const { result } = renderHook(() => usePermanentDeleteSession(), { wrapper })
    result.current.mutate('a-1')
    await waitFor(() => {
      expect(qc.getQueryData<ArchivedSession[]>(QK.archived)).toEqual([arch])
      expect(qc.getQueryData<Session[]>(QK.sessions)).toEqual([liveSess])
    })
  })
})
