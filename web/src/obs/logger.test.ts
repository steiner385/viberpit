// web/src/obs/logger.test.ts
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { ClientLogger } from './logger'

describe('ClientLogger', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

  it('batches entries and POSTs on flush', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true })
    vi.stubGlobal('fetch', fetchMock)
    const logger = new ClientLogger({ flushMs: 5000, maxBuffer: 200 })
    logger.error('boom', { sessionId: 's1' })
    logger.warn('warned')
    await logger.flush()
    expect(fetchMock).toHaveBeenCalledOnce()
    const [, init] = fetchMock.mock.calls[0]
    const body = JSON.parse(init.body)
    expect(body.entries).toHaveLength(2)
    expect(body.entries[0].level).toBe('error')
  })

  it('drops oldest beyond maxBuffer', () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true }))
    const logger = new ClientLogger({ flushMs: 5000, maxBuffer: 2 })
    logger.error('a'); logger.error('b'); logger.error('c')
    expect(logger.bufferSize).toBe(2)
  })

  it('stamps the current sessionId', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true })
    vi.stubGlobal('fetch', fetchMock)
    const logger = new ClientLogger({ flushMs: 5000, maxBuffer: 200 })
    logger.setSessionId('focused-1')
    logger.error('x')
    await logger.flush()
    const body = JSON.parse(fetchMock.mock.calls[0][1].body)
    expect(body.entries[0].sessionId).toBe('focused-1')
  })

  it('uses sendBeacon on pagehide flush', () => {
    const beacon = vi.fn().mockReturnValue(true)
    vi.stubGlobal('navigator', { sendBeacon: beacon } as unknown as Navigator)
    const logger = new ClientLogger({ flushMs: 5000, maxBuffer: 200 })
    logger.error('x')
    logger.flushBeacon()
    expect(beacon).toHaveBeenCalledOnce()
  })

  it('never throws if fetch rejects', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('net')))
    const logger = new ClientLogger({ flushMs: 5000, maxBuffer: 200 })
    logger.error('x')
    await expect(logger.flush()).resolves.toBeUndefined()
  })
})
