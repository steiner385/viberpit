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

  // `allItems` exists for the DEC-8 list filters, which narrow `items` BEFORE
  // this hook runs. `allOrderedSessions` is the close/archive fallback
  // (nextSessionAfterClose's second argument), so it must keep the rows the
  // filter removed — otherwise archiving the last match dead-ends.
  it('allOrderedSessions is built from allItems, not from the narrowed items', () => {
    const all = [mk('a'), mk('b'), mk('c')]
    const { result } = renderHook(() => useSessionOrdering([all[1]], null, all))
    expect(result.current.orderedSessions.map((s) => s.id)).toEqual(['b'])
    expect(result.current.allOrderedSessions.map((s) => s.id).sort()).toEqual(['a', 'b', 'c'])
  })

  it('allItems defaults to items, so an unfiltered caller sees no change', () => {
    const { result } = renderHook(() => useSessionOrdering([mk('a'), mk('b')]))
    expect(result.current.allOrderedSessions.map((s) => s.id))
      .toEqual(result.current.orderedSessions.map((s) => s.id))
  })

  it('search still hides from orderedSessions without touching allOrderedSessions', () => {
    const sessions = [mk('a'), mk('b')]
    const { result } = renderHook(() => useSessionOrdering(sessions, new Set(['a'])))
    expect(result.current.orderedSessions.map((s) => s.id)).toEqual(['a'])
    expect(result.current.allOrderedSessions.map((s) => s.id).sort()).toEqual(['a', 'b'])
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

  it('navItems: ungrouped has no headers (one item per session)', () => {
    const { result } = renderHook(() => useSessionOrdering([mk('a'), mk('b')]))
    expect(result.current.navItems.every((i) => i.kind === 'session')).toBe(true)
    expect(result.current.navItems).toHaveLength(2)
  })

  it('navItems: grouped emits headers; collapseGroup drops that group\'s sessions', () => {
    const { result } = renderHook(() =>
      useSessionOrdering([mk('a', { repo: 'o/r1' }), mk('b', { repo: 'o/r2' })]),
    )
    act(() => result.current.setGroupBy('repo'))
    expect(result.current.navItems.filter((i) => i.kind === 'header')).toHaveLength(2)
    const firstKey = result.current.groups[0].key
    act(() => result.current.collapseGroup(firstKey))
    const header = result.current.navItems.find((i) => i.kind === 'header' && i.key === firstKey)
    expect(header && header.kind === 'header' && header.collapsed).toBe(true)
    // collapsed group contributes no session items
    expect(result.current.navItems.some(
      (i) => i.kind === 'session' && result.current.groups[0].sessions.some((s) => s.id === i.session.id),
    )).toBe(false)
  })

  it('expandGroup re-expands a collapsed group (idempotent)', () => {
    const { result } = renderHook(() => useSessionOrdering([mk('a', { repo: 'o/r1' })]))
    act(() => result.current.setGroupBy('repo'))
    const key = result.current.groups[0].key
    act(() => result.current.collapseGroup(key))
    act(() => result.current.expandGroup(key))
    act(() => result.current.expandGroup(key)) // idempotent
    expect(result.current.collapsedKeys.has(key)).toBe(false)
  })
})
