// web/src/obs/vitals.ts
/**
 * Field Core Web Vitals — the LAB half of issue #1411's measurement is the
 * bundle budget (`web/scripts/check-bundle-budget.mjs`); this is the field half.
 *
 * A build number is not a user experience. Lighthouse on CI hardware over a
 * simulated network tells you a page got heavier, but it cannot tell you what a
 * phone on the tailnet actually saw, which is the only number the targets in
 * #1411 (p75 LCP ≤ 2.5s, INP ≤ 200ms, CLS ≤ 0.1) are stated against. So real
 * loads report real timings, segmented by route shape, device tier and browser,
 * and `GET /api/web-vitals/summary` computes the p75 the target is written in.
 *
 * ## What is and is not collected
 *
 * Every sample is `(metric, value, route SHAPE, device tier, nav type)` plus a
 * per-page-load random id. There is no URL, no query string, no identity, no
 * session id, no account name — see `routeTemplate` for how the shape is
 * derived and why it is derived from the router rather than from a pattern list.
 * The batch envelope adds the build sha and a count/byte total of the JS fetched
 * after `load`, which is #1411's "record optional chunk loads": the whole point
 * of splitting a route out of the entry chunk is that the split shows up here as
 * deferred bytes rather than as a slower LCP.
 *
 * ## Why no `rating`
 *
 * `web-vitals` reports a good/needs-improvement/poor rating alongside each
 * value. Shipping it would put the thresholds in two places — the browser bundle
 * and `server/obs/web_vitals.py`, which has to classify historical rows anyway —
 * and the two would drift the first time a threshold moved. The server owns
 * them; the client sends the number.
 */
import { BUILD_SHA } from '@/lib/version'
import type { WidthTier } from '@/shell/useViewport'

/** The metrics collected, in report order. `FID` is deliberately absent: it was
 *  retired from Core Web Vitals in favour of INP, and `web-vitals` v5+ dropped it. */
export const VITAL_METRICS = ['LCP', 'INP', 'CLS', 'TTFB', 'FCP'] as const
export type VitalMetric = (typeof VITAL_METRICS)[number]

const METRIC_SET = new Set<string>(VITAL_METRICS)

/** The shape `web-vitals`' `onLCP`/`onINP`/… hand their callback, narrowed to
 *  what is actually read. Declared structurally so the collector can be tested
 *  without the library and without a DOM. */
export interface VitalMetricReport {
  name: VitalMetric
  value: number
  navigationType?: string
  /** Present on the real library's payload; deliberately dropped — see above. */
  rating?: string
}

export interface VitalSample {
  metric: VitalMetric
  value: number
  route: string
  device: WidthTier
}

export interface VitalsContext {
  route: string
  device: WidthTier
}

export interface ChunkStats {
  lazyChunks: number
  lazyChunkBytes: number
}

export interface WebVitalsReporterOpts {
  endpoint?: string
  maxBuffer?: number
  /** Kill switch, wired to `GET /api/config`'s `webVitalsEnabled`. */
  enabled?: boolean
  clientId?: string
  chunkStats?: () => ChunkStats
  buildSha?: string
}

/**
 * Bytes and count of JavaScript fetched AFTER the page load event — i.e. the
 * code that route-level splitting moved off the critical path.
 *
 * Resource entries are read rather than counted at the import site because a
 * dynamic `import()` is not the only way a chunk arrives (a preload, a
 * `React.lazy` prefetch, the service worker all do too), and `startTime`
 * relative to `loadEventEnd` is the one definition that covers them uniformly.
 * Returns zeroes wherever the Performance API is unavailable (jsdom, Safari
 * private mode) — a missing measurement must never look like a measured zero
 * upstream, which is why the count travels with the bytes.
 */
export function measureLazyChunks(): ChunkStats {
  const empty = { lazyChunks: 0, lazyChunkBytes: 0 }
  try {
    if (typeof performance === 'undefined' || !performance.getEntriesByType) return empty
    const nav = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming | undefined
    const loadedAt = nav?.loadEventEnd
    if (!loadedAt) return empty
    let lazyChunks = 0
    let lazyChunkBytes = 0
    for (const raw of performance.getEntriesByType('resource')) {
      const e = raw as PerformanceResourceTiming
      if (e.initiatorType !== 'script' && e.initiatorType !== 'link' && e.initiatorType !== 'fetch') continue
      if (!/\.js(\?|$)/.test(e.name)) continue
      if (e.startTime <= loadedAt) continue
      lazyChunks += 1
      lazyChunkBytes += e.encodedBodySize || 0
    }
    return { lazyChunks, lazyChunkBytes }
  } catch {
    return empty
  }
}

/**
 * Buffers field vitals and ships them to `POST /api/web-vitals`.
 *
 * Deliberately NOT folded into `ClientLogger`. That sink is a log stream: every
 * entry is an incident, WARNING+ opens a GitHub issue through error triage, and
 * it carries the full URL. Vitals are the opposite — routine, aggregate-only,
 * URL-free — and a healthy page emits five of them per load. Sharing the pipe
 * would either drown the error sink or force vitals to inherit its retention
 * and its URL.
 */
export class WebVitalsReporter {
  private buf: VitalSample[] = []
  private ctx: VitalsContext | null = null
  private navType = 'navigate'
  private readonly endpoint: string
  private readonly maxBuffer: number
  private readonly clientId: string
  private readonly chunkStats: () => ChunkStats
  private readonly buildSha: string
  private enabled: boolean
  private observersClaimed = false

  constructor(opts: WebVitalsReporterOpts = {}) {
    this.endpoint = opts.endpoint ?? '/api/web-vitals'
    this.maxBuffer = opts.maxBuffer ?? 40
    this.enabled = opts.enabled ?? true
    this.clientId = opts.clientId ?? newClientId()
    this.chunkStats = opts.chunkStats ?? measureLazyChunks
    this.buildSha = opts.buildSha ?? BUILD_SHA
  }

  get bufferSize(): number { return this.buf.length }

  /**
   * Claim the one-time `web-vitals` observer registration; false if it is
   * already claimed.
   *
   * The latch lives on the reporter rather than in the component because that is
   * what it actually describes: `onLCP` and friends have no unsubscribe, so the
   * observers belong to the PAGE and feed this one reporter for as long as it
   * lives. A component-local flag would be reset by a remount and hand each
   * metric a second callback — every load counted twice, and a p75 over doubled
   * samples is wrong even though every individual value in it is right.
   */
  claimObservers(): boolean {
    if (this.observersClaimed) return false
    this.observersClaimed = true
    return true
  }

  setEnabled(enabled: boolean): void {
    this.enabled = enabled
    if (!enabled) this.buf = []
  }

  /** Called on every navigation: later samples are attributed to the new route. */
  setContext(ctx: VitalsContext): void { this.ctx = ctx }

  record(metric: VitalMetricReport): void {
    if (!this.enabled) return
    // A sample that arrives before the router has mounted has no route to be
    // attributed to. Guessing (`/`) would quietly pile every pre-mount LCP onto
    // the home route and skew exactly the number this exists to measure.
    if (!this.ctx) return
    if (!METRIC_SET.has(metric.name)) return
    if (!Number.isFinite(metric.value) || metric.value < 0) return
    if (metric.navigationType) this.navType = metric.navigationType
    this.buf.push({
      metric: metric.name,
      value: metric.value,
      route: this.ctx.route,
      device: this.ctx.device,
    })
    if (this.buf.length > this.maxBuffer) this.buf.splice(0, this.buf.length - this.maxBuffer)
  }

  private takeBatch(): string | null {
    if (!this.enabled || this.buf.length === 0) return null
    const samples = this.buf
    this.buf = []
    const { lazyChunks, lazyChunkBytes } = this.chunkStats()
    return JSON.stringify({
      samples,
      page: {
        navType: this.navType,
        appVersion: this.buildSha,
        clientId: this.clientId,
        lazyChunks,
        lazyChunkBytes,
      },
    })
  }

  async flush(): Promise<void> {
    const body = this.takeBatch()
    if (body === null) return
    try {
      await fetch(this.endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
        keepalive: true,
      })
    } catch {
      // A dropped vitals batch is a dropped measurement, never an error the user
      // should see — and reporting it through clientLogger would file an issue
      // for every offline tab.
    }
  }

  /** Last-gasp flush on pagehide. `web-vitals` reports LCP/CLS/INP finals here,
   *  so this is where most batches actually leave the browser. */
  flushBeacon(): void {
    const body = this.takeBatch()
    if (body === null) return
    try {
      navigator.sendBeacon?.(this.endpoint, new Blob([body], { type: 'application/json' }))
    } catch {
      // ignore
    }
  }
}

function newClientId(): string {
  try { return crypto.randomUUID() } catch { return `v-${Date.now()}-${Math.floor(Math.random() * 1e6)}` }
}

/** The app's one reporter. */
export const webVitalsReporter = new WebVitalsReporter()
