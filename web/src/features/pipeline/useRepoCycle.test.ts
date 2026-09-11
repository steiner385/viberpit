import { renderHook } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useRepoCycle } from './useRepoCycle'

const repos = ['o/a', 'o/b', 'o/c']

describe('useRepoCycle', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => {
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('advances to the next repo every cycleSeconds', () => {
    const focus = vi.fn()
    renderHook(() => useRepoCycle(true, 30, repos, 'o/a', focus))
    vi.advanceTimersByTime(30_000)
    expect(focus).toHaveBeenCalledWith('o/b')
  })

  it('wraps around from the last repo to the first', () => {
    const focus = vi.fn()
    renderHook(() => useRepoCycle(true, 30, repos, 'o/c', focus))
    vi.advanceTimersByTime(30_000)
    expect(focus).toHaveBeenCalledWith('o/a')
  })

  it('does not cycle when disabled', () => {
    const focus = vi.fn()
    renderHook(() => useRepoCycle(false, 30, repos, 'o/a', focus))
    vi.advanceTimersByTime(90_000)
    expect(focus).not.toHaveBeenCalled()
  })

  it('does not cycle with fewer than two repos', () => {
    const focus = vi.fn()
    renderHook(() => useRepoCycle(true, 30, ['o/a'], 'o/a', focus))
    vi.advanceTimersByTime(90_000)
    expect(focus).not.toHaveBeenCalled()
  })

  it('pauses while the tab is hidden (document.hidden)', () => {
    const focus = vi.fn()
    const spy = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
    renderHook(() => useRepoCycle(true, 30, repos, 'o/a', focus))
    vi.advanceTimersByTime(60_000)
    expect(focus).not.toHaveBeenCalled()
    spy.mockRestore()
  })
})
