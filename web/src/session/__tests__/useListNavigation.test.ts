// web/src/session/__tests__/useListNavigation.test.ts
import { describe, it, expect } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useListNavigation } from '../useListNavigation'
import type { NavItem } from '../ordering'
import type { Session } from '@/api/types'
import { mkSession } from '@/test/factories'

const sess = (id: string): Session =>
  mkSession(id, {
    accountName: 'a',
    createdAt: '2026-06-01T00:00:00Z',
    updatedAt: '2026-06-01T00:00:00Z',
  })
// header(h1) · session(a) · header(h2) · session(b)
const items: NavItem[] = [
  { kind: 'header', key: 'h1', label: 'H1', count: 1, collapsed: false },
  { kind: 'session', session: sess('a') },
  { kind: 'header', key: 'h2', label: 'H2', count: 1, collapsed: false },
  { kind: 'session', session: sess('b') },
]

describe('useListNavigation', () => {
  it('starts unhighlighted and steps down across header↔session boundaries', () => {
    const { result } = renderHook(() => useListNavigation(items))
    expect(result.current.highlightedIndex).toBe(-1)
    act(() => result.current.navigate('down')) // -1 -> 0 (header h1)
    expect(result.current.highlightedGroupKey).toBe('h1')
    expect(result.current.highlightedSession).toBeUndefined()
    act(() => result.current.navigate('down')) // 0 -> 1 (session a)
    expect(result.current.highlightedSession?.id).toBe('a')
    expect(result.current.highlightedGroupKey).toBeUndefined()
  })

  it('clamps at both ends', () => {
    const { result } = renderHook(() => useListNavigation(items))
    act(() => result.current.navigate('up')) // stays clamped at 0
    expect(result.current.highlightedIndex).toBe(0)
    act(() => result.current.setHighlightedIndex(3))
    act(() => result.current.navigate('down')) // stays at last
    expect(result.current.highlightedIndex).toBe(3)
  })

  it('re-clamps when navItems shrinks', () => {
    const { result, rerender } = renderHook(({ it }) => useListNavigation(it), {
      initialProps: { it: items },
    })
    act(() => result.current.setHighlightedIndex(3))
    rerender({ it: items.slice(0, 2) }) // now length 2 → max index 1
    expect(result.current.highlightedIndex).toBe(1)
  })

  it('highlightSession jumps to the navItem for a session id', () => {
    const { result } = renderHook(() => useListNavigation(items))
    act(() => result.current.highlightSession('b'))
    expect(result.current.highlightedIndex).toBe(3)
    expect(result.current.highlightedSession?.id).toBe('b')
  })
})
