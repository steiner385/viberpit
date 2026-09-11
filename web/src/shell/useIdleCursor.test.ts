import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { IDLE_ATTR, useIdleCursor } from './useIdleCursor'

const idle = () => document.documentElement.hasAttribute(IDLE_ATTR)

describe('useIdleCursor', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => { vi.useRealTimers(); document.documentElement.removeAttribute(IDLE_ATTR) })

  it('marks the document idle after idleMs and clears it on input', () => {
    renderHook(() => useIdleCursor(true, 5000))
    expect(idle()).toBe(false)
    act(() => { vi.advanceTimersByTime(5000) })
    expect(idle()).toBe(true)
    act(() => { window.dispatchEvent(new Event('mousemove')) })
    expect(idle()).toBe(false)
    act(() => { vi.advanceTimersByTime(5000) })
    expect(idle()).toBe(true)
  })

  it('never marks when disabled, and removes the mark on unmount', () => {
    renderHook(() => useIdleCursor(false))
    act(() => { vi.advanceTimersByTime(60_000) })
    expect(idle()).toBe(false)
    const { unmount } = renderHook(() => useIdleCursor(true, 1000))
    act(() => { vi.advanceTimersByTime(1000) })
    expect(idle()).toBe(true)
    unmount()
    expect(idle()).toBe(false)
  })
})
