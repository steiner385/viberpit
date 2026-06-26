import { describe, it, expect } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { usePageVisible } from './usePageVisible'

describe('usePageVisible', () => {
  it('tracks visibilitychange', () => {
    const { result } = renderHook(() => usePageVisible())
    expect(result.current).toBe(true) // jsdom default: 'visible'
    act(() => {
      Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true })
      document.dispatchEvent(new Event('visibilitychange'))
    })
    expect(result.current).toBe(false)
  })
})
