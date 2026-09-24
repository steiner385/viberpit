// web/src/obs/WebVitalsCollector.test.tsx
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, act } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { WebVitalsCollector } from './WebVitalsCollector'
import { WebVitalsReporter } from './vitals'
import { ViewportProvider } from '@/shell/useViewport'

const configMock = vi.fn()
vi.mock('@/api/hooks', () => ({ useConfig: () => configMock() }))

// The library is imported dynamically by the component; the real one needs
// PerformanceObserver, which jsdom does not have.
const onLCP = vi.fn()
const onINP = vi.fn()
const onCLS = vi.fn()
const onTTFB = vi.fn()
const onFCP = vi.fn()
vi.mock('web-vitals', () => ({ onLCP, onINP, onCLS, onTTFB, onFCP }))

function mount(reporter: WebVitalsReporter, path = '/sessions/abc-123') {
  return render(
    <ViewportProvider kiosk={null}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/sessions/:id" element={<WebVitalsCollector reporter={reporter} />} />
          <Route path="/roadmap" element={<WebVitalsCollector reporter={reporter} />} />
        </Routes>
      </MemoryRouter>
    </ViewportProvider>,
  )
}

describe('WebVitalsCollector', () => {
  beforeEach(() => {
    configMock.mockReturnValue({ data: { webVitalsEnabled: true } })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true }))
  })
  afterEach(() => { vi.clearAllMocks(); vi.unstubAllGlobals() })

  it('renders nothing', () => {
    const { container } = mount(new WebVitalsReporter())
    expect(container).toBeEmptyDOMElement()
  })

  it('attributes samples to the route SHAPE, never the id in the URL', async () => {
    const reporter = new WebVitalsReporter()
    mount(reporter)
    await act(async () => {})
    reporter.record({ name: 'LCP', value: 1200, navigationType: 'navigate' })
    await reporter.flush()

    const body = JSON.parse(vi.mocked(fetch).mock.calls[0][1]!.body as string)
    expect(body.samples[0].route).toBe('/sessions/:id')
    expect(JSON.stringify(body)).not.toContain('abc-123')
  })

  it('records the width tier the layout is actually in', async () => {
    const reporter = new WebVitalsReporter()
    mount(reporter)
    await act(async () => {})
    reporter.record({ name: 'LCP', value: 1200, navigationType: 'navigate' })
    await reporter.flush()

    const body = JSON.parse(vi.mocked(fetch).mock.calls[0][1]!.body as string)
    // jsdom reports no matchMedia matches, so every tier query is false → phone.
    expect(body.samples[0].device).toBe('phone')
  })

  it('subscribes to all five metrics once the config allows it', async () => {
    mount(new WebVitalsReporter())
    await act(async () => {})
    for (const fn of [onLCP, onINP, onCLS, onTTFB, onFCP]) expect(fn).toHaveBeenCalledOnce()
  })

  it('collects nothing when the edge has the kill switch off', async () => {
    configMock.mockReturnValue({ data: { webVitalsEnabled: false } })
    const reporter = new WebVitalsReporter()
    mount(reporter)
    await act(async () => {})
    expect(onLCP).not.toHaveBeenCalled()
    reporter.record({ name: 'LCP', value: 1200, navigationType: 'navigate' })
    expect(reporter.bufferSize).toBe(0)
  })

  it('collects nothing on an edge whose /config predates the flag', async () => {
    configMock.mockReturnValue({ data: { repoRoot: '/x' } })
    mount(new WebVitalsReporter())
    await act(async () => {})
    expect(onLCP).not.toHaveBeenCalled()
  })

  it('collects nothing while /config is still loading', async () => {
    configMock.mockReturnValue({ data: undefined })
    mount(new WebVitalsReporter())
    await act(async () => {})
    expect(onLCP).not.toHaveBeenCalled()
  })

  it('beacons the buffer when the page is hidden', async () => {
    const beacon = vi.fn().mockReturnValue(true)
    vi.stubGlobal('navigator', { ...navigator, sendBeacon: beacon })
    const reporter = new WebVitalsReporter()
    mount(reporter)
    await act(async () => {})
    reporter.record({ name: 'CLS', value: 0.04, navigationType: 'navigate' })

    await act(async () => { window.dispatchEvent(new Event('pagehide')) })
    expect(beacon).toHaveBeenCalledOnce()
  })
  it('subscribes once even when the component remounts (StrictMode)', async () => {
    const reporter = new WebVitalsReporter()
    const first = mount(reporter)
    await act(async () => {})
    first.unmount()
    mount(reporter)
    await act(async () => {})
    // `web-vitals` has no unsubscribe: a second registration would report every
    // load twice and skew the p75 the whole feature exists to compute.
    expect(onLCP).toHaveBeenCalledTimes(1)
  })

  it('subscribes once the config arrives, not only on the first render', async () => {
    configMock.mockReturnValue({ data: undefined })
    const reporter = new WebVitalsReporter()
    const view = mount(reporter)
    await act(async () => {})
    expect(onLCP).not.toHaveBeenCalled()

    configMock.mockReturnValue({ data: { webVitalsEnabled: true } })
    view.rerender(
      <ViewportProvider kiosk={null}>
        <MemoryRouter initialEntries={['/sessions/abc-123']}>
          <Routes>
            <Route path="/sessions/:id" element={<WebVitalsCollector reporter={reporter} />} />
          </Routes>
        </MemoryRouter>
      </ViewportProvider>,
    )
    await act(async () => {})
    expect(onLCP).toHaveBeenCalledTimes(1)
  })
})
