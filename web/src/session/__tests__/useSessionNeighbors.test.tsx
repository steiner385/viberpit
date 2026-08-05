// web/src/session/__tests__/useSessionNeighbors.test.tsx
import { describe, it, expect, beforeEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { QK } from '@/api/hooks'
import { useSessionNeighbors } from '../useSessionNeighbors'
import type { Session } from '@/api/types'

function mk(id: string, over: Partial<Session> = {}): Session {
  return {
    id, name: id, accountName: 'a', status: 'idle',
    createdAt: '2026-06-01T00:00:00Z', updatedAt: '2026-06-01T00:00:00Z', ...over,
  }
}

// Seed the react-query cache directly so useSessions resolves synchronously to
// our fixture list (no network, no act warnings).
function wrapperWith(sessions: Session[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(QK.sessions, sessions)
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
}

describe('useSessionNeighbors', () => {
  // Ordering reads persisted prefs from localStorage; clear for determinism.
  // Default sort 'status' with all-idle sessions keeps stable input order.
  beforeEach(() => window.localStorage.clear())

  it('returns prev/next for a middle session', () => {
    const { result } = renderHook(() => useSessionNeighbors('b'), {
      wrapper: wrapperWith([mk('a'), mk('b'), mk('c')]),
    })
    expect(result.current).toEqual({ prevId: 'a', nextId: 'c', index: 1, total: 3 })
  })

  it('wraps the first session prev to the last', () => {
    const { result } = renderHook(() => useSessionNeighbors('a'), {
      wrapper: wrapperWith([mk('a'), mk('b'), mk('c')]),
    })
    expect(result.current.prevId).toBe('c')
    expect(result.current.nextId).toBe('b')
  })

  it('wraps the last session next to the first', () => {
    const { result } = renderHook(() => useSessionNeighbors('c'), {
      wrapper: wrapperWith([mk('a'), mk('b'), mk('c')]),
    })
    expect(result.current.nextId).toBe('a')
    expect(result.current.prevId).toBe('b')
  })

  it('returns null neighbors for a single session', () => {
    const { result } = renderHook(() => useSessionNeighbors('a'), {
      wrapper: wrapperWith([mk('a')]),
    })
    expect(result.current).toEqual({ prevId: null, nextId: null, index: 0, total: 1 })
  })

  it('returns null neighbors when the session is not in the list', () => {
    const { result } = renderHook(() => useSessionNeighbors('z'), {
      wrapper: wrapperWith([mk('a'), mk('b')]),
    })
    expect(result.current).toEqual({ prevId: null, nextId: null, index: -1, total: 2 })
  })

  it("falls back to the full ordering when the session's group is collapsed", () => {
    // Group by status: 'a' (idle → your-turn tier) sits in a collapsed group, so
    // it is absent from the VISIBLE ordering — the chevrons must still navigate,
    // walking the full ordering instead of disappearing.
    window.localStorage.setItem('ccswitch.sessionOrder', JSON.stringify({ groupBy: 'status', sortBy: 'status' }))
    window.localStorage.setItem('ccswitch.groupCollapse', JSON.stringify(['your-turn']))
    const { result } = renderHook(() => useSessionNeighbors('a'), {
      wrapper: wrapperWith([mk('a'), mk('b', { status: 'in_progress' })]),
    })
    expect(result.current.prevId).toBe('b')
    expect(result.current.nextId).toBe('b')
  })
})
