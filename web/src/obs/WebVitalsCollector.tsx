/**
 * Mounts field Core Web Vitals collection (#1411).
 *
 * Renders nothing. It exists as a component rather than a call in `main.tsx`
 * because two of the three things a sample needs are React state:
 *
 *   - the DEVICE TIER comes from `useViewport()`, and `shell/useViewport` is the
 *     app's only permitted layout `matchMedia` reader (its own test greps for
 *     offenders). Reading the breakpoints here would be a second answer to "is
 *     this a phone" that could disagree with the layout the user actually got.
 *   - the ROUTE SHAPE comes from the router's own `useParams()`, so the
 *     identifiers redacted from it are exactly the ones the route table declared
 *     (see `obs/routeTemplate`).
 *
 * The third — whether to collect at all — comes from `/api/config`, so the
 * kill switch stops the PerformanceObservers, not just the ingest.
 *
 * ## Why the library is dynamically imported
 *
 * `web-vitals` is small, but it is only ever needed after first paint, and the
 * whole point of the issue is that the entry chunk is too big. Importing it
 * lazily keeps it out of the critical path AND makes it one of the deferred
 * chunks the collector then reports — the measurement pays for itself.
 */
import * as React from 'react'
import { useLocation, useParams } from 'react-router-dom'
import { useConfig } from '@/api/hooks'
import { useViewport } from '@/shell/useViewport'
import { routeTemplate } from './routeTemplate'
import { webVitalsReporter, type VitalMetricReport, type WebVitalsReporter } from './vitals'

/** How often buffered samples are shipped while a tab stays open.
 *
 *  `web-vitals` finalises LCP/CLS/INP at pagehide, which is where most batches
 *  leave — but a kiosk display never hides, so without a timer its TTFB/FCP
 *  would sit in the buffer forever and the wall-display experience, the one
 *  nobody is watching, would be the one that goes unmeasured. */
const FLUSH_MS = 20_000

export function WebVitalsCollector({
  reporter = webVitalsReporter,
}: { reporter?: WebVitalsReporter }) {
  const { data: config } = useConfig()
  // Absent means an edge that predates the endpoint; see ConfigInfo.
  const enabled = config?.webVitalsEnabled === true
  const { width } = useViewport()
  const { pathname } = useLocation()
  // `useParams()` is a fresh object every render, so the STRING is the effect
  // dependency — depending on the object would re-run the effect on every render
  // of every page.
  const route = routeTemplate(pathname, useParams())

  // The context has to be set BEFORE the observers can fire, and it has to be
  // current on every navigation, so it is a layout effect rather than an effect:
  // a sample taken during the commit that follows a route change belongs to the
  // new route.
  React.useLayoutEffect(() => {
    reporter.setContext({ route, device: width })
  }, [reporter, route, width])

  React.useEffect(() => {
    reporter.setEnabled(enabled)
    if (!enabled) return

    void import('web-vitals').then(({ onCLS, onFCP, onINP, onLCP, onTTFB }) => {
      // The latch is claimed HERE, after the chunk lands, rather than before the
      // import: StrictMode mounts, tears down and re-mounts, so a latch claimed
      // up front would be held by the pass that was torn down before its chunk
      // arrived and nothing would ever register in development. It lives on the
      // reporter because the observers do — see `claimObservers`.
      if (!reporter.claimObservers()) return
      const record = (m: VitalMetricReport) => reporter.record(m)
      // `reportAllChanges` is deliberately left at its default: one final value
      // per metric per page is what a p75 is computed from, and reporting every
      // intermediate LCP candidate would weight a load by how many candidates it
      // happened to have.
      onLCP(record); onINP(record); onCLS(record); onTTFB(record); onFCP(record)
    }).catch(() => {
      // A chunk that will not load is a deploy-in-progress, not an incident.
    })

    const timer = setInterval(() => { void reporter.flush() }, FLUSH_MS)
    const onHide = () => reporter.flushBeacon()
    const onVisibility = () => { if (document.visibilityState === 'hidden') reporter.flushBeacon() }
    window.addEventListener('pagehide', onHide)
    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      clearInterval(timer)
      window.removeEventListener('pagehide', onHide)
      document.removeEventListener('visibilitychange', onVisibility)
      // Unmount is a teardown, not a page hide: flush what is buffered rather
      // than dropping it. (In production this only runs on a full app teardown.)
      reporter.flushBeacon()
    }
  }, [reporter, enabled])

  return null
}
