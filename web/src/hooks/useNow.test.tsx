import { renderHook, act } from '@testing-library/react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { useNow } from './useNow'

function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { value: state, configurable: true })
  document.dispatchEvent(new Event('visibilitychange'))
}

describe('useNow', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    setVisibility('visible')
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('reports the current time after mount and advances on the interval', () => {
    vi.setSystemTime(1_000_000)
    const { result } = renderHook(() => useNow(1000))
    // flush the initial setTimeout(0) catch-up
    act(() => { vi.advanceTimersByTime(0) })
    expect(result.current).toBe(1_000_000)
    // advance exactly one interval; fake timers advance Date.now() in sync,
    // so the tick at +1000ms reads 1_001_000
    act(() => { vi.advanceTimersByTime(1000) })
    expect(result.current).toBe(1_001_000)
  })

  it('freezes while the tab is hidden and catches up when visible again', () => {
    vi.setSystemTime(2_000_000)
    const { result } = renderHook(() => useNow(1000))
    // flush mount catch-up
    act(() => { vi.advanceTimersByTime(0) })
    expect(result.current).toBe(2_000_000)
    act(() => { setVisibility('hidden') })
    // advance 50 seconds while hidden — no tick should fire
    act(() => { vi.advanceTimersByTime(50_000) })
    expect(result.current).toBe(2_000_000) // frozen — no tick while hidden
    // become visible again — effect re-runs, setTimeout(0) fires immediately
    act(() => { setVisibility('visible') })
    act(() => { vi.advanceTimersByTime(0) })
    expect(result.current).toBe(2_050_000) // immediate catch-up on return
  })
})

it('does not tick while active is false', () => {
  vi.useFakeTimers()
  const { result } = renderHook(() => useNow(1000, false))
  expect(result.current).toBe(0)
  act(() => { vi.advanceTimersByTime(3000) })
  expect(result.current).toBe(0)   // frozen — the gate suppressed all ticks
  vi.useRealTimers()
})
