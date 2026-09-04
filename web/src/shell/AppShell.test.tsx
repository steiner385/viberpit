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
// `regionCounts` reads the whole document (the overlay root is outside `#root`)
// — shared with `kiosk.integration.test.tsx` rather than copied into it.
import { APP_REGION_PREFIX, appRegionCounts, presentAppRegions, regionCounts } from '@/test/regions'
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

/**
 * …except kiosk, which never gets to BE on `/sessions`.
 *
 * `AppShell`'s `KioskPageGuard` redirects a kiosk URL off any section that is
 * not `showInKiosk` (verified read-only), so `/sessions?drawer=help` in kiosk
 * lands on the kiosk home, `/pipeline` — a section with no list and not
 * Sessions. The expectation follows the route that actually rendered rather
 * than pretending kiosk is still on Sessions: kiosk zeroes `app.list` and
 * `app.dock` anyway, so using `SESSIONS_CTX` here would pass for the wrong
 * reason and would keep passing if the guard were deleted.
 */
const KIOSK_CTX: Omit<RegionContext, 'mode'> = {
  hasList: false,
  itemOpen: false,
  inSessions: false,
  drawer: 'help',
}

const ctxFor = (mode: LayoutMode) => (mode === 'kiosk' ? KIOSK_CTX : SESSIONS_CTX)

/** The probe page each mode lands on from `/sessions?drawer=help`. */
const landingFor = (mode: LayoutMode) => (mode === 'kiosk' ? 'page-pipeline' : 'page-sessions')

beforeEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
})

afterEach(() => {
  window.history.replaceState(null, '', '/')
})

// ── tests ────────────────────────────────────────────────────────────────────

// The three set-equality cases below are scoped to `app.*` (DEC-1). They compare
// the document against `REGION_VISIBILITY`, which answers ONLY for the regions
// the shell places; page-owned namespaces (`session.header`, Task 10) are not in
// the matrix and never were. Unscoped, the arrival of the first page region
// would have reddened all three for a reason none of them is about — and the fix
// would have been to add a row to the matrix for a region the shell cannot show
// or hide. The scoping is narrow on purpose: it drops a NAMESPACE, not a
// predicate. Every `app.*` id is still compared by equality in both directions,
// so a shell region that goes missing, appears when the matrix says it should
// not, or mounts twice still fails — see the deliberate-break note in
// `test/regions.ts` and `visibilityMatrix.test.tsx`'s "REGION_IDS is app.* only",
// which is what stops a shell region from escaping the scope by being renamed.
describe('AppShell — the regions on screen match the matrix', () => {
  it.each(LAYOUT_MODES)('renders exactly the regions %s calls for', async (mode) => {
    renderShell({ path: '/sessions?drawer=help', mode })
    await screen.findByTestId(landingFor(mode))

    expect(presentAppRegions()).toEqual(expectedRegions(mode, ctxFor(mode)).slice().sort())
  })

  it.each(LAYOUT_MODES)('renders no app.* data-region outside RegionId in %s', async (mode) => {
    renderShell({ path: '/sessions?drawer=help', mode })
    await screen.findByTestId(landingFor(mode))

    const known: readonly string[] = REGION_IDS
    const unknown = [...appRegionCounts().keys()].filter((id) => !known.includes(id))
    expect(unknown, `unrecognised shell regions: ${unknown.join(', ')}`).toEqual([])
  })

  it.each(LAYOUT_MODES)('every data-region is a shell region or a namespaced page one in %s', async (mode) => {
    // The other half of the scoping: nothing may carry a BARE `data-region`
    // that is neither the shell's nor a namespaced page region. Without this,
    // scoping to `app.*` above would let a typo'd or unnamespaced marker
    // (`app-list`, `main`) sit in the document unnoticed forever.
    renderShell({ path: '/sessions?drawer=help', mode })
    await screen.findByTestId(landingFor(mode))

    const stray = [...regionCounts().keys()].filter((id) => !/^[a-z]+\.[a-z]+$/.test(id))
    expect(stray, `data-region values outside <namespace>.<name>: ${stray.join(', ')}`).toEqual([])
  })

  it.each(LAYOUT_MODES)('in %s mode every region mounts at most once', async (mode) => {
    renderShell({ path: '/sessions?drawer=help', mode })
    await screen.findByTestId(landingFor(mode))

    const counts = appRegionCounts()
    // The lower bound. Without it "at most once" is satisfied by a shell that
    // rendered nothing at all.
    expect([...counts.keys()].sort()).toEqual(expectedRegions(mode, ctxFor(mode)).slice().sort())

    // `app.rail` is legitimately TWO nodes on phone — `TopBar` (data-rail="top")
    // and `NavBar` (data-rail="bottom") — see Task 8. Everything else is one.
    const allowed = (id: string) => (mode === 'phone' && id === 'app.rail' ? 2 : 1)
    for (const [id, n] of counts) expect(n, `${id} in ${mode}`).toBeLessThanOrEqual(allowed(id))
  })

  it('scopes on the shell namespace the matrix is written in', () => {
    // Pins the scoping's own premise: `REGION_IDS` — the list `expectedRegions`
    // is derived from — is entirely inside the prefix the assertions filter on.
    // A shell region renamed out of `app.*` would otherwise vanish from every
    // comparison above instead of failing.
    for (const id of REGION_IDS) expect(id.startsWith(APP_REGION_PREFIX), id).toBe(true)
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

  /**
   * The other half of the same rule, recorded because it is a DECISION (S4):
   * `useDrawerPreservingTo` is applied by `NavItem` and by nothing else, so a
   * content navigation — opening a session from the list — drops `?drawer=` and
   * closes the drawer. `drawerParam.ts` carries the reasoning; this pins the
   * behaviour so it cannot change by accident in either direction.
   */
  it('closes the drawer on a content navigation — preservation is nav-only', async () => {
    const user = userEvent.setup()
    renderShell({
      path: '/sessions?drawer=help',
      mode: 'desktop',
      extraQueries: [MEDIA.drawerPush],
    })
    await screen.findByTestId('page-sessions')
    // The lower bound: the drawer really is open before the click, so "closed
    // after" is not "never opened".
    expect(regionCounts().get('app.drawer')).toBe(1)

    await user.click(await screen.findByRole('button', { name: 'Open session First' }))

    await screen.findByTestId('page-session')
    expect(regionCounts().has('app.drawer')).toBe(false)
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
    await screen.findByTestId(landingFor(mode))
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

  /**
   * Push mode is the ordinary desktop (≥1280px) shape of the drawer, and it was
   * the one shape no axe pass ever rendered: the four cases above never pass
   * `extraQueries`, so `drawerPushes` is false in all of them and the drawer is
   * always an overlay — which mounts no resizer at all. `DrawerHost.test.tsx`'s
   * own pass runs on the panel alone, so it cannot see a sibling either.
   *
   * The resizer is a focusable `role="separator"`, so its PLACEMENT is the whole
   * question — the identical defect Task 17 found and fixed for `ContextList`,
   * whose handle sat outside every landmark until it moved inside the `<aside>`.
   */
  it('has no axe violations with the drawer in push mode', async () => {
    renderShell({ path: '/sessions?drawer=help', mode: 'desktop', extraQueries: [MEDIA.drawerPush] })
    await screen.findByTestId('page-sessions')
    await withSessionRows('desktop')

    // The lower bound: push mode really is on, so a pass over a document with no
    // resizer in it cannot be mistaken for a clean one.
    const resizer = screen.getByRole('separator', { name: 'Resize drawer' })
    expect(resizer).toBeVisible()
    expect(regionCounts().get('app.drawer')).toBe(1)

    expect(await axe(document.body, AXE_OPTIONS)).toHaveNoViolations()
  })
})
