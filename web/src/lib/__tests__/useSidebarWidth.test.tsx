/**
 * R15: the list-column width is ONE store, not a copy per component.
 *
 * `ContextList` (the visible column) and Settings › Appearance both hold it, so
 * a write through either has to reach the other without a reload — which is a
 * property of the store, asserted here, rather than of either caller.
 */
import { describe, it, expect } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useSidebarWidth, writeSidebarWidth } from '../useSidebarWidth'
import { SIDEBAR_DEFAULT, SIDEBAR_MAX, SIDEBAR_STORAGE_KEY } from '../sidebarWidth'

describe('useSidebarWidth', () => {
  it('starts at the stored width and every subscriber sees one write', () => {
    localStorage.setItem(SIDEBAR_STORAGE_KEY, '400')
    const a = renderHook(() => useSidebarWidth())
    const b = renderHook(() => useSidebarWidth())
    expect(a.result.current[0]).toBe(400)

    act(() => a.result.current[1](SIDEBAR_DEFAULT))
    expect(a.result.current[0]).toBe(SIDEBAR_DEFAULT)
    expect(b.result.current[0]).toBe(SIDEBAR_DEFAULT)
    expect(localStorage.getItem(SIDEBAR_STORAGE_KEY)).toBe(String(SIDEBAR_DEFAULT))
  })

  it('clamps what the setter is given', () => {
    const { result } = renderHook(() => useSidebarWidth())
    act(() => result.current[1](SIDEBAR_MAX + 200))
    expect(result.current[0]).toBe(SIDEBAR_MAX)
  })

  it('notifies subscribers of a write made outside React', () => {
    const { result } = renderHook(() => useSidebarWidth())
    act(() => writeSidebarWidth(360))
    expect(result.current[0]).toBe(360)
  })
})
