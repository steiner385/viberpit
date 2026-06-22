import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useMediaQuery } from '../useMediaQuery'

afterEach(() => { vi.unstubAllGlobals() })

describe('useMediaQuery', () => {
  it('returns false when matchMedia is unavailable (jsdom default)', () => {
    // jsdom has no matchMedia; ensure it is absent
    vi.stubGlobal('matchMedia', undefined)
    const { result } = renderHook(() => useMediaQuery('(min-width: 1024px)'))
    expect(result.current).toBe(false)
  })

  it('returns true when matchMedia reports a match', () => {
    vi.stubGlobal('matchMedia', (q: string) => ({
      matches: true,
      media: q,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      onchange: null,
      dispatchEvent: () => false,
    }))
    const { result } = renderHook(() => useMediaQuery('(min-width: 1024px)'))
    expect(result.current).toBe(true)
  })
})
