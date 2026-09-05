import { describe, it, expect } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { readRailExpanded, writeRailExpanded, useRailExpanded, RAIL_EXPANDED_KEY } from './railExpanded'

describe('railExpanded', () => {
  it('defaults collapsed, persists, and every subscriber sees a write', () => {
    expect(readRailExpanded()).toBe(false)
    const a = renderHook(() => useRailExpanded())
    const b = renderHook(() => useRailExpanded())
    act(() => a.result.current[1](true))
    expect(b.result.current[0]).toBe(true)
    expect(localStorage.getItem(RAIL_EXPANDED_KEY)).toBe('true')
    writeRailExpanded(false)
    expect(readRailExpanded()).toBe(false)
  })
})
