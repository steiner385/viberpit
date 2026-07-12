import { renderHook } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useKioskCycle } from '../useKioskCycle'

const repos = ['o/a', 'o/b', 'o/c']

describe('useKioskCycle', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => {
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('advances to the next repo every cycleSeconds', () => {
    const focus = vi.fn()
    renderHook(() => useKioskCycle(true, 30, repos, 'o/a', focus))
    vi.advanceTimersByTime(30_000)
    expect(focus).toHaveBeenCalledWith('o/b')
  })

  it('wraps around from the last repo to the first', () => {
    const focus = vi.fn()
    renderHook(() => useKioskCycle(true, 30, repos, 'o/c', focus))
    vi.advanceTimersByTime(30_000)
    expect(focus).toHaveBeenCalledWith('o/a')
  })

  it('does not cycle when kiosk is false', () => {
    const focus = vi.fn()
    renderHook(() => useKioskCycle(false, 30, repos, 'o/a', focus))
    vi.advanceTimersByTime(90_000)
    expect(focus).not.toHaveBeenCalled()
  })

  it('does not cycle with fewer than two repos', () => {
    const focus = vi.fn()
    renderHook(() => useKioskCycle(true, 30, ['o/a'], 'o/a', focus))
    vi.advanceTimersByTime(90_000)
    expect(focus).not.toHaveBeenCalled()
  })

  it('pauses while the tab is hidden (document.hidden)', () => {
    const focus = vi.fn()
    const spy = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
    renderHook(() => useKioskCycle(true, 30, repos, 'o/a', focus))
    vi.advanceTimersByTime(60_000)
    expect(focus).not.toHaveBeenCalled()
    spy.mockRestore()
  })
})
