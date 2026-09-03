// AppShell — the region-singleton guard (umbrella §7) and the matrix on the
// rendered DOM.
//
// ## Why every assertion here has a positive lower bound
//
// "Every region mounts at most once" is trivially satisfiable by rendering
// NOTHING: a shell that throws, a `matchMedia` stub the harness forgot, or a
// route that never matches all produce zero `[data-region]` nodes and a green
// "≤ 1 of each". Three of the last five tasks on this project shipped a control
// that passed for exactly that shape of reason (an edit that was a silent no-op,
// a break that crashed the route into an error page, a portal fallback to
// `<body>`). So the region set is asserted as an EQUALITY against
// `REGION_VISIBILITY` — the regions that must be present, and no others — and
// the singleton check runs on top of that. Neither half can pass on an empty
// document.
//
// ## Why the real route table
//
// The routes come from `buildRoutes()` with only the page ELEMENTS swapped for
// probes. A hand-written table here would let `/sessions/:id` be spelled
// differently from the app and the whole itemOpen column would go on passing
// against an approximation. (Same reason, same shape, as `PageHost.test.tsx`.)
//
// ## Scope note on axe
//
// jsdom applies no stylesheet, so the axe passes below cover roles, names and
// ARIA wiring ONLY. Its `color-contrast` rule always returns `incomplete` and
// `toHaveNoViolations()` inspects `violations` alone — a green assertion is NOT
// evidence of adequate contrast. Contrast is checked separately by
// src/__tests__/contrast.test.ts against the tokens in index.css.
import * as React from 'react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { axe, toHaveNoViolations } from 'jest-axe'
import { createMemoryRouter, RouterProvider, type RouteObject } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

expect.extend(toHaveNoViolations)

import type { Session } from '@/api/types'

// ── typed fixtures (R10: real values, never `as unknown as T`) ────────────────

function session(over: Partial<Session> = {}): Session {
  return {
    id: 'a',
    name: 'First',
    accountName: 'alice',
    status: 'idle',
    createdAt: '2026-06-19T10:00:00Z',
    prs: [],
    ...over,
  }
}

const SESSIONS: Session[] = [session({ id: 'a', name: 'First' }), session({ id: 'b', name: 'Second' })]

// ── the fake daemon ──────────────────────────────────────────────────────────
//
// The api CLIENT is faked, not the hooks: every hook the shell mounts —
// `useSessions`, `useNavBadges`'s five queries, `useConfig`, the banners — stays
// real, so a region that stops asking for its data fails here. `[]` is the
// default answer, matching `App.test.tsx`'s harness.

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      get: vi.fn(async (path: string): Promise<unknown> => {
        if (path === '/sessions' || path === '/sessions?viewing=1') return SESSIONS
        if (path === '/fleet/hosts') return { hosts: [] }
        if (path === '/config') return { adoptionEnabled: false }
        return []
      }),
      post: vi.fn(async () => ({})),
      put: vi.fn(async () => ({})),
      patch: vi.fn(async () => ({})),
      del: vi.fn(async () => undefined),
    },
  }
})

// The websocket is the only genuinely external edge of the real runtime provider.
vi.mock('@/ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/ws/client')>()
  return {
    ...actual,
    useSessionSocket: () => ({
      attach: vi.fn(), detach: vi.fn(), interrupt: vi.fn(), prompt: vi.fn(),
      sendInterrupt: vi.fn(), permissionResponse: vi.fn(), answerQuestion: vi.fn(),
      setPermissionMode: vi.fn(), cancelQueued: vi.fn(), command: vi.fn(),
      activity: vi.fn(), on: vi.fn(), off: vi.fn(),
      connectionState: 'connected' as const, socket: null,
    }),
  }
})

import { LAYOUT_MODE_QUERIES, MEDIA, stubMatchMedia } from '@/test/matchMedia'
import { ThemeProvider } from '@/shell/theme'
import { buildRoutes } from '@/shell/routes'
import { LAYOUT_MODES, type LayoutMode } from './useViewport'
import { AppShell, REGION_IDS, REGION_VISIBILITY, type RegionContext, type RegionId } from './AppShell'

// ── harness ──────────────────────────────────────────────────────────────────

function Page({ name }: { name: string }) {
  return <div data-testid={`page-${name}`}>{name}</div>
}

/** Pages swapped for probes; every path, redirect and the catch-all stay as
 *  `routes.tsx` declares them. */
const PROBE_PAGES: Record<string, string> = {
  '/sessions': 'sessions',
  '/sessions/:id': 'session',
  '/pipeline': 'pipeline',
}

function probeRoutes(): RouteObject[] {
  const [root] = buildRoutes(<AppShell />)
  const layout = root.children![0]
  const pages = layout.children!.map((route): RouteObject => {
    const name = route.path ? PROBE_PAGES[route.path] : undefined
    return name ? { path: route.path, element: <Page name={name} /> } : route
  })
  // Every probed path must still exist in the real table — a rename in
  // routes.tsx must redden this file, not silently drop a probe.
  const paths = new Set(layout.children!.map((r) => r.path))
  for (const p of Object.keys(PROBE_PAGES)) {
    if (!paths.has(p)) throw new Error(`probeRoutes: ${p} is no longer a route in routes.tsx`)
  }
  return [{ ...root, children: [{ ...layout, children: pages }] }]
}

interface RenderOpts {
  path?: string
  mode?: LayoutMode
  /** Extra media queries on top of the mode's — e.g. the drawer-push threshold. */
  extraQueries?: readonly string[]
}

function renderShell({ path = '/sessions', mode = 'desktop', extraQueries = [] }: RenderOpts = {}) {
  stubMatchMedia([...LAYOUT_MODE_QUERIES[mode], ...extraQueries])
  // `readKioskConfig()` reads `window.location.search`, not the router's — a
  // wall display is configured by the URL it was launched with.
  if (mode === 'kiosk') window.history.replaceState(null, '', '/?kiosk=1')

  const router = createMemoryRouter(probeRoutes(), { initialEntries: [path] })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <ThemeProvider>
        <RouterProvider router={router} />
      </ThemeProvider>
    </QueryClientProvider>,
  )
}

/**
 * Every `data-region` node in the DOCUMENT, not just in the render container:
 * `app.overlays` is stamped on the portal root and `app.drawer`'s overlay panel
 * portals into it, both of which live outside `#root`.
 */
function regionCounts(): Map<string, number> {
  const counts = new Map<string, number>()
  for (const el of document.querySelectorAll('[data-region]')) {
    const id = el.getAttribute('data-region')!
    counts.set(id, (counts.get(id) ?? 0) + 1)
  }
  return counts
}

/** The regions the matrix says must be on screen, for the context under test. */
function expectedRegions(mode: LayoutMode, ctx: Omit<RegionContext, 'mode'>): RegionId[] {
  return REGION_IDS.filter((id) => REGION_VISIBILITY[id]({ mode, ...ctx }))
}

/** `/sessions` with a drawer open — the context every mode is rendered in below. */
const SESSIONS_CTX: Omit<RegionContext, 'mode'> = {
  hasList: true,
  itemOpen: false,
  inSessions: true,
  drawer: 'help',
}

beforeEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
})

afterEach(() => {
  window.history.replaceState(null, '', '/')
})

// ── tests ────────────────────────────────────────────────────────────────────

describe('AppShell — the regions on screen match the matrix', () => {
  it.each(LAYOUT_MODES)('renders exactly the regions %s calls for', async (mode) => {
    renderShell({ path: '/sessions?drawer=help', mode })
    await screen.findByTestId('page-sessions')

    const present = [...regionCounts().keys()].sort()
    expect(present).toEqual(expectedRegions(mode, SESSIONS_CTX).slice().sort())
  })

  it.each(LAYOUT_MODES)('renders no data-region outside RegionId in %s', async (mode) => {
    renderShell({ path: '/sessions?drawer=help', mode })
    await screen.findByTestId('page-sessions')

    const known: readonly string[] = REGION_IDS
    const unknown = [...regionCounts().keys()].filter((id) => !known.includes(id))
    expect(unknown, `unrecognised regions: ${unknown.join(', ')}`).toEqual([])
  })

  it.each(LAYOUT_MODES)('in %s mode every region mounts at most once', async (mode) => {
    renderShell({ path: '/sessions?drawer=help', mode })
    await screen.findByTestId('page-sessions')

    const counts = regionCounts()
    // The lower bound. Without it "at most once" is satisfied by a shell that
    // rendered nothing at all.
    expect([...counts.keys()].sort()).toEqual(expectedRegions(mode, SESSIONS_CTX).slice().sort())

    // `app.rail` is legitimately TWO nodes on phone — `TopBar` (data-rail="top")
    // and `NavBar` (data-rail="bottom") — see Task 8. Everything else is one.
    const allowed = (id: string) => (mode === 'phone' && id === 'app.rail' ? 2 : 1)
    for (const [id, n] of counts) expect(n, `${id} in ${mode}`).toBeLessThanOrEqual(allowed(id))
  })

  it('renders the phone rail as TopBar + NavBar, one of each', async () => {
    renderShell({ path: '/sessions', mode: 'phone' })
    await screen.findByTestId('page-sessions')

    expect(document.querySelectorAll('[data-region="app.rail"]')).toHaveLength(2)
    expect(document.querySelectorAll('[data-rail="top"]')).toHaveLength(1)
    expect(document.querySelectorAll('[data-rail="bottom"]')).toHaveLength(1)
    expect(document.querySelectorAll('[data-rail="left"]')).toHaveLength(0)
  })

  it.each(['tablet', 'desktop'] as const)('renders the %s rail as one NavRail', async (mode) => {
    renderShell({ path: '/sessions', mode })
    await screen.findByTestId('page-sessions')

    expect(document.querySelectorAll('[data-rail="left"]')).toHaveLength(1)
    expect(document.querySelectorAll('[data-rail="top"]')).toHaveLength(0)
    expect(document.querySelectorAll('[data-rail="bottom"]')).toHaveLength(0)
  })
})

describe('AppShell — the conditional regions', () => {
  it('drops app.list on phone once an item is open, and keeps it on desktop', async () => {
    const phone = renderShell({ path: '/sessions/a', mode: 'phone' })
    await screen.findByTestId('page-session')
    expect(regionCounts().has('app.list')).toBe(false)
    // …and the page it made room for is on screen, not merely un-hidden.
    expect(screen.getByTestId('page-session')).toBeVisible()
    phone.unmount()

    renderShell({ path: '/sessions/a', mode: 'desktop' })
    await screen.findByTestId('page-session')
    expect(regionCounts().get('app.list')).toBe(1)
  })

  it('shows the list and the dock together on phone, with the page hidden behind them', async () => {
    renderShell({ path: '/sessions', mode: 'phone' })
    await screen.findByTestId('page-sessions')

    // Both regions are present …
    expect(regionCounts().get('app.list')).toBe(1)
    expect(regionCounts().get('app.dock')).toBe(1)
    // … the prompt bar is genuinely on screen (this is how a phone user starts
    // a session — it must not go down with the page) …
    expect(document.querySelector('[data-region="app.dock"]')).toBeVisible()
    // … and the routed page behind them is not.
    expect(screen.getByTestId('page-sessions')).not.toBeVisible()
  })

  it('drops app.dock outside the Sessions section', async () => {
    renderShell({ path: '/pipeline', mode: 'desktop' })
    await screen.findByTestId('page-pipeline')

    expect(regionCounts().has('app.dock')).toBe(false)
    expect(regionCounts().has('app.list')).toBe(false)
    expect(regionCounts().get('app.main')).toBe(1)
  })

  it('renders app.drawer only for a recognised ?drawer=', async () => {
    const bogus = renderShell({ path: '/sessions?drawer=nope', mode: 'desktop' })
    await screen.findByTestId('page-sessions')
    expect(regionCounts().has('app.drawer')).toBe(false)
    bogus.unmount()

    renderShell({ path: '/sessions?drawer=help', mode: 'desktop' })
    await screen.findByTestId('page-sessions')
    expect(regionCounts().get('app.drawer')).toBe(1)
  })
})

describe('AppShell — an open drawer survives a section change (§4.2)', () => {
  it('carries ?drawer= onto every nav target and keeps the drawer open', async () => {
    const user = userEvent.setup()
    renderShell({
      path: '/sessions?drawer=help',
      mode: 'desktop',
      // Push mode: an inline column rather than a modal overlay, so the nav rail
      // beside it is a normal click target.
      extraQueries: [MEDIA.drawerPush],
    })
    await screen.findByTestId('page-sessions')

    const pipeline = screen.getByRole('link', { name: /^Pipeline/ })
    expect(pipeline).toHaveAttribute('href', expect.stringContaining('drawer=help'))

    await user.click(pipeline)
    await screen.findByTestId('page-pipeline')
    expect(regionCounts().get('app.drawer')).toBe(1)
  })

  it('leaves nav targets alone when no drawer is open', async () => {
    renderShell({ path: '/sessions', mode: 'desktop' })
    await screen.findByTestId('page-sessions')

    expect(screen.getByRole('link', { name: /^Pipeline/ })).toHaveAttribute('href', '/pipeline')
  })
})

describe('AppShell — accessibility', () => {
  /**
   * NO rule is disabled — `region` ("all page content is inside a landmark")
   * included.
   *
   * Task 16 had to turn `region` off: `app.main` was a `<div>`, because
   * `ReviewView`, `SubSessionView` and `App.tsx` each declared their own
   * `<main>` and promoting the region would have nested two. Task 17 deleted
   * `App.tsx`'s shell fork and demoted the other two, so `PageHost` is now the
   * single `main` landmark and the rule holds. It is asserted rather than
   * suppressed precisely so that re-introducing a page-level `<main>`, or
   * dropping the landmark from the region, reddens this file.
   */
  const AXE_OPTIONS = {}

  /**
   * Wait for the list to have ROWS, not just to be mounted.
   *
   * `DispatchBar`'s textarea carries `aria-controls="session-listbox"`, and
   * `SessionList` only renders that id once it has sessions — while
   * `useSessions` is loading it renders a skeleton instead. So for one frame
   * the dock points at an element that does not exist
   * (`aria-valid-attr-value`). That is a real, pre-existing defect: it ships
   * today in both `Dashboard` and `DesktopShell`, which is why
   * `PromptDock.test.tsx` stands in a bare `<div id="session-listbox">`. It is
   * not the shell's to fix — the dock does not own that id — so this waits past
   * the loading frame rather than asserting the transient violation away.
   */
  async function withSessionRows(mode: LayoutMode) {
    if (mode === 'kiosk') return // kiosk renders neither the list nor the dock
    await waitFor(() => expect(document.getElementById('session-listbox')).not.toBeNull())
  }

  it.each(LAYOUT_MODES)('has no axe violations in %s', async (mode) => {
    renderShell({ path: '/sessions?drawer=help', mode })
    await screen.findByTestId('page-sessions')
    await withSessionRows(mode)
    // The overlay root sits outside the render container; assert on the whole
    // body so an open drawer's panel is included in the pass.
    //
    // ONE axe run, not a `waitFor` around one. `waitFor` retries its callback
    // inside a 1000ms budget, and a single axe pass over the whole shell — page,
    // list, dock, rail and an open drawer — does not reliably finish inside it
    // when the suite runs in parallel. That made this case fail with "Timed out
    // in waitFor" rather than with a violation: a red test that says nothing
    // about accessibility. The waits it was standing in for are explicit above
    // (`findByTestId`, then `withSessionRows` for the dock's `aria-controls`
    // target), so there is nothing left for a retry to fix.
    expect(await axe(document.body, AXE_OPTIONS)).toHaveNoViolations()
  })
})
