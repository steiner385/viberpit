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

  it('filters sessions to matchedIds before grouping', () => {
    const sessions = [mk('a'), mk('b'), mk('c')]
    const { result } = renderHook(() =>
      useSessionOrdering(sessions, new Set(['a', 'c'])),
    )
    expect(result.current.orderedSessions.map((s) => s.id)).toEqual(['a', 'c'])
    expect(result.current.groups.flatMap((g) => g.sessions).map((s) => s.id)).toEqual(['a', 'c'])
  })

  it('treats null matchedIds as no filter', () => {
    const sessions = [mk('a'), mk('b')]
    const { result } = renderHook(() => useSessionOrdering(sessions, null))
    expect(result.current.orderedSessions.map((s) => s.id)).toEqual(['a', 'b'])
  })

  it('yields empty groups when matchedIds excludes everything', () => {
    const { result } = renderHook(() => useSessionOrdering([mk('a')], new Set<string>()))
    expect(result.current.orderedSessions).toEqual([])
    expect(result.current.groups).toEqual([])
  })

  it('toggleCollapse adds then removes a key, returning new sets', () => {
    const { result } = renderHook(() => useSessionOrdering([mk('s1')]))
    expect(result.current.collapsedKeys.has('busy')).toBe(false)
    act(() => result.current.toggleCollapse('busy'))
    expect(result.current.collapsedKeys.has('busy')).toBe(true)
    act(() => result.current.toggleCollapse('busy'))
    expect(result.current.collapsedKeys.has('busy')).toBe(false)
  })

  it('orderedSessions excludes sessions in a collapsed group (status mode)', () => {
    // s1 idle -> tier "your-turn"; s2 in_progress -> tier "busy"
    const { result } = renderHook(() =>
      useSessionOrdering([mk('s1', { status: 'idle' }), mk('s2', { status: 'in_progress' })]),
    )
    act(() => result.current.setGroupBy('status'))
    expect(result.current.orderedSessions.map((s) => s.id).sort()).toEqual(['s1', 's2'])
    act(() => result.current.toggleCollapse('busy'))
    expect(result.current.orderedSessions.map((s) => s.id)).toEqual(['s1'])
  })

  it('persists collapsed keys across a remount', () => {
    const first = renderHook(() => useSessionOrdering([mk('s1')]))
    act(() => first.result.current.toggleCollapse('busy'))
    first.unmount()
    const second = renderHook(() => useSessionOrdering([mk('s1')]))
    expect(second.result.current.collapsedKeys.has('busy')).toBe(true)
  })
})
