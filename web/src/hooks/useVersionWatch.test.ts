import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { useVersionWatch } from './useVersionWatch'
import * as wsClient from '@/ws/client'

// Minimal fake socket exposing the on/off event API.
function makeFakeSocket() {
  const handlers: Record<string, Set<(...a: unknown[]) => void>> = {}
  return {
    on: (e: string, cb: (...a: unknown[]) => void) => { (handlers[e] ??= new Set()).add(cb) },
    off: (e: string, cb: (...a: unknown[]) => void) => { handlers[e]?.delete(cb) },
    emit: (e: string, ...a: unknown[]) => { handlers[e]?.forEach((cb) => cb(...a)) },
  }
}

beforeEach(() => { vi.restoreAllMocks() })
afterEach(() => { vi.unstubAllGlobals() })

describe('useVersionWatch', () => {
  it('flips updateAvailable when /healthz reports a different sha on reconnect', async () => {
    const fake = makeFakeSocket()
    vi.spyOn(wsClient, 'getSessionSocket').mockReturnValue(fake as unknown as ReturnType<typeof wsClient.getSessionSocket>)
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ sha: 'newsha1' }) })))

    const { result } = renderHook(() => useVersionWatch({ enabled: true, buildSha: 'oldsha0' }))
    expect(result.current).toBe(false)

    fake.emit('connectionStateChange', 'connected')
    await waitFor(() => expect(result.current).toBe(true))
  })

  it('stays false when sha matches', async () => {
    const fake = makeFakeSocket()
    vi.spyOn(wsClient, 'getSessionSocket').mockReturnValue(fake as unknown as ReturnType<typeof wsClient.getSessionSocket>)
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ sha: 'samesha' }) })))

    const { result } = renderHook(() => useVersionWatch({ enabled: true, buildSha: 'samesha' }))
    fake.emit('connectionStateChange', 'connected')
    await new Promise((r) => setTimeout(r, 10))
    expect(result.current).toBe(false)
  })

  it('does nothing when disabled (dev/test default)', async () => {
    const fake = makeFakeSocket()
    vi.spyOn(wsClient, 'getSessionSocket').mockReturnValue(fake as unknown as ReturnType<typeof wsClient.getSessionSocket>)
    const fetchSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)

    renderHook(() => useVersionWatch({ enabled: false, buildSha: 'oldsha0' }))
    fake.emit('connectionStateChange', 'connected')
    await new Promise((r) => setTimeout(r, 10))
    expect(fetchSpy).not.toHaveBeenCalled()
  })
})
