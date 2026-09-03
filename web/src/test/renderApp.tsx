/**
 * `renderApp` — mount the REAL app (shell + route table) at a URL, in a chosen
 * layout mode.
 *
 * ## Why a shared harness
 *
 * Task 18 migrated a dozen suites off `Dashboard` / `DesktopShell`, and every
 * one of them needs the same five lines: stub the layout media queries, build
 * the real routes around `AppShell`, put a memory router at a path, wrap it in a
 * QueryClient and a ThemeProvider. Written per file, those five lines drift —
 * that is exactly how `matchMedia` came to be stubbed four different ways before
 * `test/matchMedia.ts` (see its header). The mode → query mapping lives there,
 * with the breakpoints; only the composition lives here.
 *
 * ## What it deliberately does NOT do
 *
 * It does not mock the API. `vi.mock` is hoisted per test file and each suite
 * needs its own fake daemon, so the mock stays with the test that depends on it.
 * The harness only guarantees a `QueryClientProvider` is present.
 *
 * `AppShell.test.tsx` keeps its own variant on purpose: it swaps the page
 * elements for probes so it can assert the region matrix without dragging real
 * pages in. This one renders the real pages, which is what a routing or
 * workspace test is actually about.
 *
 * There is deliberately no `rerender` helper. The route element `<AppShell/>` is
 * built once inside `buildRoutes`, so re-rendering the tree hands React the
 * identical element and it bails out — a "refresh" that silently does nothing. A
 * test that needs the shell to re-read a mocked hook should move the router
 * (`router.navigate`), which is how the app re-renders in life.
 */
import { render, type RenderResult } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AppShell } from '@/shell/AppShell'
import { buildRoutes } from '@/shell/routes'
import { ThemeProvider } from '@/shell/theme'
import type { LayoutMode } from '@/shell/useViewport'
import { LAYOUT_MODE_QUERIES, stubMatchMedia } from './matchMedia'

export interface RenderAppOptions {
  /** The URL to start at. Ignored when `entries` is given. */
  path?: string
  /** A full history stack, for tests that need Back to have somewhere to go. */
  entries?: string[]
  /** Which entry of `entries` is current. Defaults to the last. */
  index?: number
  /**
   * The layout mode. Phone is the default because it is the narrowest — a test
   * that does not say which layout it means gets the one with the fewest
   * regions, so an assertion that depends on the desktop list column has to ask
   * for desktop explicitly rather than passing by accident.
   */
  mode?: LayoutMode
  /** Extra media queries on top of the mode's — e.g. the drawer-push threshold. */
  extraQueries?: readonly string[]
  /** A pre-seeded QueryClient, when a test needs to prime the cache. */
  queryClient?: QueryClient
}

export interface RenderAppResult extends RenderResult {
  router: ReturnType<typeof createMemoryRouter>
  /** The current pathname, read from the router (not from a probe component). */
  path: () => string
  /** The current search string, including the leading `?`. */
  search: () => string
  /**
   * How the router reached the current entry: `'PUSH'`, `'REPLACE'` or `'POP'`.
   *
   * This is what makes "closing an overlay adds no history entry" (§4.4)
   * assertable directly rather than by inference.
   */
  historyAction: () => string
}

export function renderApp({
  path = '/sessions',
  entries,
  index,
  mode = 'phone',
  extraQueries = [],
  queryClient,
}: RenderAppOptions = {}): RenderAppResult {
  stubMatchMedia([...LAYOUT_MODE_QUERIES[mode], ...extraQueries])
  // `readKioskConfig()` reads `window.location.search`, not the router's — a
  // wall display is configured by the URL it was launched with.
  if (mode === 'kiosk') window.history.replaceState(null, '', '/?kiosk=1')

  const initialEntries = entries ?? [path]
  const router = createMemoryRouter(buildRoutes(<AppShell />), {
    initialEntries,
    initialIndex: index ?? initialEntries.length - 1,
  })
  const qc = queryClient ?? new QueryClient({ defaultOptions: { queries: { retry: false } } })

  const view = render(
    <QueryClientProvider client={qc}>
      <ThemeProvider>
        <RouterProvider router={router} />
      </ThemeProvider>
    </QueryClientProvider>,
  )

  return {
    ...view,
    router,
    path: () => router.state.location.pathname,
    search: () => router.state.location.search,
    historyAction: () => router.state.historyAction,
  }
}
