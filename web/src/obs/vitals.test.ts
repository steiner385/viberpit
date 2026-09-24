// @vitest-environment node
// web/src/obs/vitals.test.ts
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { WebVitalsReporter, VITAL_METRICS } from './vitals'

function reporter(overrides: ConstructorParameters<typeof WebVitalsReporter>[0] = {}) {
  return new WebVitalsReporter({ endpoint: '/api/web-vitals', ...overrides })
}

describe('WebVitalsReporter', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

  it('stamps each sample with the current route and device tier', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true })
    vi.stubGlobal('fetch', fetchMock)
    const r = reporter()
    r.setContext({ route: '/sessions/:id', device: 'phone' })
    r.record({ name: 'LCP', value: 1234.5, navigationType: 'navigate' })
    await r.flush()

    const body = JSON.parse(fetchMock.mock.calls[0][1].body)
    expect(body.samples).toHaveLength(1)
    expect(body.samples[0]).toMatchObject({
      metric: 'LCP', value: 1234.5, route: '/sessions/:id', device: 'phone',
    })
  })

  it('carries the route each sample was taken on, not the latest one', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true })
    vi.stubGlobal('fetch', fetchMock)
    const r = reporter()
    r.setContext({ route: '/sessions', device: 'desktop' })
    r.record({ name: 'LCP', value: 900, navigationType: 'navigate' })
    r.setContext({ route: '/roadmap', device: 'desktop' })
    r.record({ name: 'INP', value: 120, navigationType: 'navigate' })
    await r.flush()

    const { samples } = JSON.parse(fetchMock.mock.calls[0][1].body)
    expect(samples.map((s: { route: string }) => s.route)).toEqual(['/sessions', '/roadmap'])
  })

  it('never sends a rating — the server owns the thresholds', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true })
    vi.stubGlobal('fetch', fetchMock)
    const r = reporter()
    r.setContext({ route: '/', device: 'desktop' })
    r.record({ name: 'CLS', value: 0.02, navigationType: 'navigate', rating: 'good' })
    await r.flush()

    const { samples } = JSON.parse(fetchMock.mock.calls[0][1].body)
    expect(samples[0]).not.toHaveProperty('rating')
  })

  it('reports the page envelope once per batch', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true })
    vi.stubGlobal('fetch', fetchMock)
    const r = reporter({ chunkStats: () => ({ lazyChunks: 3, lazyChunkBytes: 40960 }) })
    r.setContext({ route: '/', device: 'tablet' })
    r.record({ name: 'TTFB', value: 80, navigationType: 'reload' })
    await r.flush()

    const { page } = JSON.parse(fetchMock.mock.calls[0][1].body)
    expect(page).toMatchObject({ navType: 'reload', lazyChunks: 3, lazyChunkBytes: 40960 })
    expect(page.appVersion).toBeTruthy()
  })

  it('does not POST an empty batch', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true })
    vi.stubGlobal('fetch', fetchMock)
    await reporter().flush()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('drops samples beyond the buffer cap rather than growing without bound', () => {
    const r = reporter({ maxBuffer: 2 })
    r.setContext({ route: '/', device: 'desktop' })
    for (let i = 0; i < 5; i++) r.record({ name: 'INP', value: i, navigationType: 'navigate' })
    expect(r.bufferSize).toBe(2)
  })

  it('ignores a metric it does not collect, and non-finite values', () => {
    const r = reporter()
    r.setContext({ route: '/', device: 'desktop' })
    r.record({ name: 'FID' as (typeof VITAL_METRICS)[number], value: 10, navigationType: 'navigate' })
    r.record({ name: 'LCP', value: Number.NaN, navigationType: 'navigate' })
    r.record({ name: 'LCP', value: Number.POSITIVE_INFINITY, navigationType: 'navigate' })
    expect(r.bufferSize).toBe(0)
  })

  it('drops a sample taken before any route context exists', () => {
    const r = reporter()
    r.record({ name: 'LCP', value: 500, navigationType: 'navigate' })
    expect(r.bufferSize).toBe(0)
  })

  it('uses sendBeacon for the last-gasp flush', () => {
    const beacon = vi.fn().mockReturnValue(true)
    vi.stubGlobal('navigator', { sendBeacon: beacon } as unknown as Navigator)
    const r = reporter()
    r.setContext({ route: '/', device: 'desktop' })
    r.record({ name: 'CLS', value: 0.05, navigationType: 'navigate' })
    r.flushBeacon()
    expect(beacon).toHaveBeenCalledOnce()
    expect(r.bufferSize).toBe(0)
  })

  it('never throws when the network is down', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
    const r = reporter()
    r.setContext({ route: '/', device: 'desktop' })
    r.record({ name: 'LCP', value: 700, navigationType: 'navigate' })
    await expect(r.flush()).resolves.toBeUndefined()
  })

  it('stops collecting once disabled', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true })
    vi.stubGlobal('fetch', fetchMock)
    const r = reporter({ enabled: false })
    r.setContext({ route: '/', device: 'desktop' })
    r.record({ name: 'LCP', value: 700, navigationType: 'navigate' })
    await r.flush()
    expect(r.bufferSize).toBe(0)
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
