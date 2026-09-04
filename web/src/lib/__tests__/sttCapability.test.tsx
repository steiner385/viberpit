import { describe, it, expect, afterEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { getServerSttEnabled, setServerSttEnabled, useServerSttEnabled } from '../sttCapability'

afterEach(() => { setServerSttEnabled(false) })

describe('sttCapability', () => {
  it('defaults to false and notifies subscribers on change', () => {
    expect(getServerSttEnabled()).toBe(false)
    const { result } = renderHook(() => useServerSttEnabled())
    expect(result.current).toBe(false)
    act(() => { setServerSttEnabled(true) })
    expect(result.current).toBe(true)
  })
})
