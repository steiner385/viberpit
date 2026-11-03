import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'

const mutate = vi.fn()
vi.mock('@/api/hooks', () => ({
  useMarkSessionViewed: () => ({ mutate }),
}))

import { useAutoMarkViewed, VIEWED_DEBOUNCE_MS } from './useAutoMarkViewed'
import { PENDING_ID_PREFIX } from '@/lib/pendingSessions'

function setVisibility(state: DocumentVisibilityState) {
  Object.defineProperty(document, 'visibilityState', { value: state, configurable: true })
  act(() => { document.dispatchEvent(new Event('visibilitychange')) })
}

type Props = { id: string; updatedAt: string | null | undefined }

function mount(initial: Props) {
  return renderHook(({ id, updatedAt }: Props) => useAutoMarkViewed(id, updatedAt), { initialProps: initial })
}

describe('useAutoMarkViewed (D-10)', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    mutate.mockClear()
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
  })
  afterEach(() => {
    vi.useRealTimers()
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
  })

  it('debounces to 2 s', () => {
    expect(VIEWED_DEBOUNCE_MS).toBe(2000)
  })

  it('posts viewed once when the session opens', () => {
    const { rerender } = mount({ id: 's1', updatedAt: 't1' })
    expect(mutate).toHaveBeenCalledTimes(1)
    expect(mutate).toHaveBeenCalledWith('s1')
    rerender({ id: 's1', updatedAt: 't1' })
    act(() => { vi.advanceTimersByTime(10_000) })
    expect(mutate).toHaveBeenCalledTimes(1)
  })

  it('posts again after the debounce when updatedAt changes while visible', () => {
    const { rerender } = mount({ id: 's1', updatedAt: 't1' })
    mutate.mockClear()
    rerender({ id: 's1', updatedAt: 't2' })
    act(() => { vi.advanceTimersByTime(VIEWED_DEBOUNCE_MS - 1) })
    expect(mutate).not.toHaveBeenCalled()
    // A second turn end inside the window restarts it: one post, not two.
    rerender({ id: 's1', updatedAt: 't3' })
    act(() => { vi.advanceTimersByTime(VIEWED_DEBOUNCE_MS - 1) })
    expect(mutate).not.toHaveBeenCalled()
    act(() => { vi.advanceTimersByTime(1) })
    expect(mutate).toHaveBeenCalledTimes(1)
    expect(mutate).toHaveBeenCalledWith('s1')
  })

  it('posts nothing while the tab is hidden, then once when it is visible again', () => {
    const { rerender } = mount({ id: 's1', updatedAt: 't1' })
    mutate.mockClear()
    setVisibility('hidden')
    rerender({ id: 's1', updatedAt: 't2' })
    act(() => { vi.advanceTimersByTime(10_000) })
    rerender({ id: 's1', updatedAt: 't3' })
    act(() => { vi.advanceTimersByTime(10_000) })
    expect(mutate).not.toHaveBeenCalled()
    setVisibility('visible')
    expect(mutate).toHaveBeenCalledTimes(1)
    act(() => { vi.advanceTimersByTime(10_000) })
    expect(mutate).toHaveBeenCalledTimes(1)
  })

  it('cancels a pending debounce when the tab hides', () => {
    const { rerender } = mount({ id: 's1', updatedAt: 't1' })
    mutate.mockClear()
    rerender({ id: 's1', updatedAt: 't2' })
    setVisibility('hidden')
    act(() => { vi.advanceTimersByTime(10_000) })
    expect(mutate).not.toHaveBeenCalled()
  })

  it('posts nothing when it opens on a hidden tab', () => {
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true })
    mount({ id: 's1', updatedAt: 't1' })
    act(() => { vi.advanceTimersByTime(10_000) })
    expect(mutate).not.toHaveBeenCalled()
  })

  it('posts once for the new session when the open session changes', () => {
    const { rerender } = mount({ id: 's1', updatedAt: 't1' })
    mutate.mockClear()
    rerender({ id: 's2', updatedAt: 'u1' })
    act(() => { vi.advanceTimersByTime(10_000) })
    expect(mutate).toHaveBeenCalledTimes(1)
    expect(mutate).toHaveBeenCalledWith('s2')
  })

  it('skips a pending (optimistic) session', () => {
    const { rerender } = mount({ id: `${PENDING_ID_PREFIX}1`, updatedAt: 't1' })
    rerender({ id: `${PENDING_ID_PREFIX}1`, updatedAt: 't2' })
    act(() => { vi.advanceTimersByTime(10_000) })
    setVisibility('hidden')
    setVisibility('visible')
    expect(mutate).not.toHaveBeenCalled()
  })
})
