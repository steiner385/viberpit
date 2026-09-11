import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { useWakeLock } from './useWakeLock'

const release = vi.fn().mockResolvedValue(undefined)
const request = vi.fn()

function stubWakeLock(impl: () => Promise<{ release: () => Promise<void> }>) {
  request.mockImplementation(impl)
  Object.defineProperty(navigator, 'wakeLock', { value: { request }, configurable: true })
}

describe('useWakeLock', () => {
  beforeEach(() => { request.mockReset(); release.mockClear() })
  afterEach(() => { delete (navigator as { wakeLock?: unknown }).wakeLock })

  it('requests a screen lock on mount, re-requests on visibility, releases on unmount', async () => {
    stubWakeLock(() => Promise.resolve({ release }))
    const { unmount } = renderHook(() => useWakeLock(true))
    expect(request).toHaveBeenCalledWith('screen')
    await waitFor(() => expect(request).toHaveBeenCalledTimes(1))
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    expect(request).toHaveBeenCalledTimes(2)
    unmount()
    await waitFor(() => expect(release).toHaveBeenCalled())
  })

  it('does nothing when disabled or when the API is missing', () => {
    stubWakeLock(() => Promise.resolve({ release }))
    renderHook(() => useWakeLock(false))
    expect(request).not.toHaveBeenCalled()
    delete (navigator as { wakeLock?: unknown }).wakeLock
    expect(() => renderHook(() => useWakeLock(true))).not.toThrow()
  })

  it('swallows a refused request', async () => {
    stubWakeLock(() => Promise.reject(new Error('NotAllowedError')))
    const { unmount } = renderHook(() => useWakeLock(true))
    await act(async () => { await Promise.resolve() })
    expect(() => unmount()).not.toThrow()
  })
})
