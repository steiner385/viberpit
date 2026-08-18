import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { useEpochRestart } from './useEpochRestart'
import * as wsClient from '@/ws/client'

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

describe('useEpochRestart', () => {
  it('starts as false', () => {
    const fake = makeFakeSocket()
    vi.spyOn(wsClient, 'getSessionSocket').mockReturnValue(fake as unknown as ReturnType<typeof wsClient.getSessionSocket>)
    const { result } = renderHook(() => useEpochRestart())
    expect(result.current).toBe(false)
  })

  it('flips to true on epochRestart event', async () => {
    const fake = makeFakeSocket()
    vi.spyOn(wsClient, 'getSessionSocket').mockReturnValue(fake as unknown as ReturnType<typeof wsClient.getSessionSocket>)
    const { result } = renderHook(() => useEpochRestart())

    fake.emit('epochRestart')
    await waitFor(() => expect(result.current).toBe(true))
  })

  it('clears back to false on reconnect when daemon is healthy', async () => {
    const fake = makeFakeSocket()
    vi.spyOn(wsClient, 'getSessionSocket').mockReturnValue(fake as unknown as ReturnType<typeof wsClient.getSessionSocket>)
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ daemon: 'ok' }) })))

    const { result } = renderHook(() => useEpochRestart())

    fake.emit('epochRestart')
    await waitFor(() => expect(result.current).toBe(true))

    fake.emit('connectionStateChange', 'connected')
    await waitFor(() => expect(result.current).toBe(false))
  })

  it('stays true on reconnect when daemon is still unavailable', async () => {
    const fake = makeFakeSocket()
    vi.spyOn(wsClient, 'getSessionSocket').mockReturnValue(fake as unknown as ReturnType<typeof wsClient.getSessionSocket>)
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ daemon: 'unavailable' }) })))

    const { result } = renderHook(() => useEpochRestart())

    fake.emit('epochRestart')
    await waitFor(() => expect(result.current).toBe(true))

    fake.emit('connectionStateChange', 'connected')
    await new Promise((r) => setTimeout(r, 20))
    expect(result.current).toBe(true)
  })

  it('stays true when /healthz fetch fails', async () => {
    const fake = makeFakeSocket()
    vi.spyOn(wsClient, 'getSessionSocket').mockReturnValue(fake as unknown as ReturnType<typeof wsClient.getSessionSocket>)
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('network error') }))

    const { result } = renderHook(() => useEpochRestart())

    fake.emit('epochRestart')
    await waitFor(() => expect(result.current).toBe(true))

    fake.emit('connectionStateChange', 'connected')
    await new Promise((r) => setTimeout(r, 20))
    expect(result.current).toBe(true)
  })
})
