import { describe, it, expect, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useSessionOrdering } from '../useSessionOrdering'
import type { Session } from '@/api/types'

function mk(id: string, over: Partial<Session> = {}): Session {
  return {
    id, name: id, accountName: 'a', status: 'idle',
    createdAt: '2026-06-01T00:00:00Z', updatedAt: '2026-06-01T00:00:00Z', ...over,
  }
}

describe('useSessionOrdering', () => {
  beforeEach(() => window.localStorage.clear())

  it('defaults to none/status and flattens groups into orderedSessions', () => {
    const sessions = [mk('a'), mk('b')]
    const { result } = renderHook(() => useSessionOrdering(sessions))
    expect(result.current.groupBy).toBe('none')
    expect(result.current.sortBy).toBe('status')
    expect(result.current.orderedSessions.map((s) => s.id))
      .toEqual(result.current.groups.flatMap((g) => g.sessions).map((s) => s.id))
  })

  it('setGroupBy re-derives groups and persists', () => {
    const { result } = renderHook(() => useSessionOrdering([mk('a', { repo: 'o/r' })]))
    act(() => result.current.setGroupBy('repo'))
    expect(result.current.groups[0].label).toBe('o/r')
    expect(window.localStorage.getItem('ccswitch.sessionOrder')).toContain('repo')
  })

  it('restores persisted selection on mount', () => {
    window.localStorage.setItem('ccswitch.sessionOrder', JSON.stringify({ groupBy: 'status', sortBy: 'name' }))
    const { result } = renderHook(() => useSessionOrdering([mk('a')]))
    expect(result.current.groupBy).toBe('status')
    expect(result.current.sortBy).toBe('name')
  })
})
