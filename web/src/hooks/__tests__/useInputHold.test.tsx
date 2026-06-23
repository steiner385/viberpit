import { describe, it, expect } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useInputHold } from '../useInputHold'

describe('useInputHold', () => {
  it('degrades to not-holding when the socket is unavailable (jsdom)', () => {
    // jsdom rejects the relative '/ws' WebSocket URL, so the hook must degrade safely.
    const { result } = renderHook(() => useInputHold())
    expect(result.current.holding).toBe(false)
    expect(result.current.count).toBe(0)
  })
})
