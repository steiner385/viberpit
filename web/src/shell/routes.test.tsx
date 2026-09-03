// The route table — what each URL resolves to, and what every legacy URL
// becomes.
//
// ## Why the pages are mocked, and what that costs
//
// The subject here is the TABLE, not the pages: every page is stubbed with a
// component that renders one testid. That keeps the file from needing the whole
// API surface mocked, and — the reason it is safe — it makes the assertions
// SHARPER rather than weaker. Each assertion names the page it expects.
//
// That naming is deliberate. "The wrong page is not shown" is satisfied by an
// error boundary, by the catch-all redirect, and by a blank render alike, so a
// table can be catastrophically broken and still pass it. Every test below
// asserts a specific page testid, so a route that silently falls through to `*`
// fails with "unable to find page-pipeline" — not with a green tick.
//
// If `routes.tsx` ever imports a component this file does not stub, the real one
// renders and blows up loudly on its missing API. That is the intended failure:
// a stub that silently stands in for something else is what these mocks must
// never become.
//
// ## Two harnesses, on purpose
//
// Everything above the "44-cell matrix" divider mounts the table under
// `TestLayout`, a stand-in that renders an `<Outlet/>` and the URL. That keeps
// the redirect and page tests about the TABLE.
//
// The matrix at the bottom mounts the REAL `AppShell` as the layout, because its
// claim is not about the table alone: "every route renders in every layout mode"
// is only worth asserting if the layout mode is load-bearing. Under `TestLayout`
// the four modes would be four identical renders and the matrix would be eleven
// tests wearing a forty-four-cell costume.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, matchRoutes, Outlet, RouterProvider, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// ── page stubs ───────────────────────────────────────────────────────────────
//
// Named `page-<navId>` for the section pages, so the NAV_DEFS guard below can
// derive the id it expects from the nav map instead of a second hand-written
// list.

function stub(name: string) {
  return () => <div data-testid={`page-${name}`}>{name}</div>
}

vi.mock('@/features/sessions/WelcomePane', () => ({ WelcomePane: stub('sessions') }))
vi.mock('@/features/sessions/MainPaneSession', () => ({
  MainPaneSession: ({ sessionId }: { sessionId: string }) => (
    <div data-testid="page-session-detail">{sessionId}</div>
  ),
}))
vi.mock('@/components/session/SubSessionView', () => ({
  SubSessionView: ({ sessionId, agentId }: { sessionId: string; agentId: string }) => (
    <div data-testid="page-subagent">{`${sessionId}/${agentId}`}</div>
  ),
}))
vi.mock('@/components/ReviewView', () => ({
  ReviewView: ({ sessionId }: { sessionId: string }) => (
    <div data-testid="page-review">{sessionId}</div>
  ),
}))
vi.mock('@/components/ArchivedManager', () => ({ ArchivedManager: stub('archived') }))
vi.mock('@/pipeline/PipelineTab', () => ({ PipelineTab: stub('pipeline') }))
vi.mock('@/automations/AutomationsTab', () => ({ AutomationsTab: stub('automations') }))
vi.mock('@/initiatives/InitiativesTab', () => ({ InitiativesTab: stub('initiatives') }))
vi.mock('@/projects/ProjectsTab', () => ({ ProjectsTab: stub('projects') }))
vi.mock('@/components/InsightsView', () => ({ InsightsView: stub('insights') }))
vi.mock('@/components/settings/SettingsView', () => ({
  // Renders the section it was handed, so `/settings/:section` is asserted to
  // arrive at the component and not merely to stop redirecting.
  SettingsView: ({ section }: { section?: string }) => (
    <div data-testid="page-settings">{section ?? '(none)'}</div>
  ),
}))

// The shell the matrix mounts needs a daemon. The api CLIENT is faked, not the
// hooks, so every region that asks for data still asks for it — same shape, and
// the same reason, as `AppShell.test.tsx`'s harness. `[]` is the default answer;
// no assertion below depends on the contents, so the pages stay stubs.
vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      get: vi.fn(async (path: string): Promise<unknown> => {
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

// The only genuinely external edge of the real session runtime `PageHost` mounts.
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

import { ErrorBoundary } from '@/components/ErrorBoundary'
import { LAYOUT_MODE_QUERIES, stubMatchMedia } from '@/test/matchMedia'
import { ThemeProvider } from '@/shell/theme'
import { AppShell } from './AppShell'
import { NAV_DEFS, navDefForPath } from './navDefs'
import { buildRoutes } from './routes'
import { LAYOUT_MODES, type LayoutMode } from './useViewport'

// ── harness ──────────────────────────────────────────────────────────────────

/** Stands in for `AppShell`: renders the routed page and reports the URL. */
function TestLayout() {
  const { pathname, search } = useLocation()
  return (
    <div>
      <div data-testid="url">{pathname + search}</div>
      <Outlet />
    </div>
  )
}

function routerAt(entries: string[], index = entries.length - 1) {
  return createMemoryRouter(buildRoutes(<TestLayout />), {
    initialEntries: entries,
    initialIndex: index,
  })
}

/**
 * A `QueryClientProvider` around the table.
 *
 * `SessionsPage` calls `useArchiveAdvance` (Task 17, ported from `App.tsx`'s
 * deleted `SessionRoute`), which reads `useSessions` / `useArchivedSessions` to
 * work out which session the detail view advances to. `main.tsx` has always
 * mounted this provider above `App`, so the routed pages are never without one
 * in the app; without it here every route rendered react-router's error page
 * instead of the table under test. The client is per-render with retries off:
 * the queries resolve to their `[]` defaults and no page depends on their
 * contents, so the pages stay stubs and this stays a test of the TABLE.
 */
function renderAt(path: string) {
  const router = routerAt([path])
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return router
}

const url = () => screen.getByTestId('url').textContent

// ── legacy and index redirects ───────────────────────────────────────────────

describe('route table — redirects', () => {
  it.each([
    ['/', '/sessions'],
    ['/session/abc', '/sessions/abc'],
    ['/session/abc/subagent/x', '/sessions/abc/agents/x'],
    ['/review/abc', '/archived/abc'],
    ['/accounts', '/settings/accounts'],
    ['/automation', '/settings/automation'],
    ['/settings', '/settings/general'],
    ['/settings?section=accounts', '/settings/accounts'],
    // A stale link can carry a blank section. Passed through, `/settings/%20`
    // is a page that exists, renders General, and leaves a nonsense URL in the
    // bar. (The empty case self-heals — `/settings/` normalises back to
    // `/settings`, and the second pass has no search left to misread — so the
    // whitespace case is the one that pins the guard.)
    ['/settings?section=', '/settings/general'],
    ['/settings?section=%20%20', '/settings/general'],
    // `/library` has defaulted to Plugins since the Library tab became a
    // Settings section; the bookmark keeps landing there.
    ['/library', '/settings/plugins'],
    ['/library?section=skills', '/settings/skills'],
    // ?dialog=settings was the modal-era deep link, and it could sit on any path.
    ['/sessions?dialog=settings', '/settings/general'],
    ['/pipeline?dialog=settings', '/settings/general'],
    ['/nope', '/sessions'],
    ['/deep/unknown/path', '/sessions'],
  ])('%s → %s', (from, to) => {
    renderAt(from)
    expect(url()).toBe(to)
  })

  it('lands the legacy session link on the session, not just on the URL', () => {
    // The URL assertion above cannot tell a working redirect from one that lands
    // on a route that renders nothing; this one names the page and the id.
    renderAt('/session/abc')
    expect(screen.getByTestId('page-session-detail')).toHaveTextContent('abc')
  })

  it('lands the legacy review link on the read-only transcript', () => {
    renderAt('/review/abc')
    expect(screen.getByTestId('page-review')).toHaveTextContent('abc')
  })

  it('lands the legacy sub-agent link on that sub-agent', () => {
    renderAt('/session/abc/subagent/x')
    expect(screen.getByTestId('page-subagent')).toHaveTextContent('abc/x')
  })

  it('lands ?dialog=settings on Settings with the param dropped', () => {
    renderAt('/pipeline?dialog=settings')
    expect(screen.getByTestId('page-settings')).toHaveTextContent('general')
    expect(url()).not.toContain('dialog')
  })

  it('leaves a path alone when ?dialog= is not the settings dialog', () => {
    // The gate sits above every route, so an over-broad match would swallow the
    // new-session dialog link too.
    renderAt('/sessions?dialog=new')
    expect(url()).toBe('/sessions?dialog=new')
    expect(screen.getByTestId('page-sessions')).toBeInTheDocument()
  })

  it('redirects replace rather than push, so Back is not bounced forward', async () => {
    const router = routerAt(['/pipeline', '/session/abc'])
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
    expect(url()).toBe('/sessions/abc')

    await act(() => router.navigate(-1))

    // A PUSHED redirect would leave /session/abc in the stack: Back would land
    // on it and be redirected forward to /sessions/abc again — the loop the
    // user cannot escape. Replaced, Back reaches the page before it.
    expect(url()).toBe('/pipeline')
    expect(screen.getByTestId('page-pipeline')).toBeInTheDocument()
  })
})

// ── the pages ────────────────────────────────────────────────────────────────

describe('route table — pages', () => {
  it.each([
    ['/sessions', 'page-sessions'],
    ['/sessions/abc', 'page-session-detail'],
    ['/sessions/abc/agents/x', 'page-subagent'],
    ['/pipeline', 'page-pipeline'],
    ['/automations', 'page-automations'],
    ['/initiatives', 'page-initiatives'],
    ['/projects', 'page-projects'],
    ['/insights', 'page-insights'],
    ['/archived', 'page-archived'],
    ['/archived/abc', 'page-review'],
    ['/settings/general', 'page-settings'],
  ])('%s renders %s without redirecting', (path, testid) => {
    renderAt(path)
    expect(url()).toBe(path)
    expect(screen.getByTestId(testid)).toBeInTheDocument()
  })

  it('renders the layout around the page, once', () => {
    renderAt('/pipeline')
    expect(screen.getAllByTestId('url')).toHaveLength(1)
  })

  it('hands the path section to Settings (DEC-14)', () => {
    renderAt('/settings/accounts')
    expect(screen.getByTestId('page-settings')).toHaveTextContent('accounts')
  })

  it('carries :id into the open session', () => {
    renderAt('/sessions/abc')
    expect(screen.getByTestId('page-session-detail')).toHaveTextContent('abc')
  })

  it('carries both params into the sub-agent page', () => {
    renderAt('/sessions/abc/agents/x')
    expect(screen.getByTestId('page-subagent')).toHaveTextContent('abc/x')
  })

  /**
   * The scroll-wrapper invariant, inherited from `automationTab.test.tsx`.
   *
   * `PageHost` is a clipped flex column (`flex-1 min-h-0`), so a page whose
   * content can grow must own an `overflow-y-auto` region or it overflows with
   * nowhere to scroll — an expanded automation card, a long settings section.
   * `Dashboard` expressed this on its tab panels; the wrappers in `routes.tsx`
   * express it now, which is why this assertion lives here. (The old test also
   * required `min-h-0`; the wrappers use `h-full` instead, because the clamped
   * height comes from `PageHost` rather than from a sibling flex row.)
   *
   * Pipeline, Initiatives and Projects are deliberately absent: each manages its
   * own inner scrolling and would fail this as written.
   */
  it.each(['/automations', '/insights', '/archived', '/settings/automation'])(
    '%s owns a vertical scroll container',
    (path) => {
      renderAt(path)
      const page = screen.getByTestId(/^page-/).parentElement!
      expect(page.className, path).toContain('overflow-y-auto')
      expect(page.className, path).toContain('h-full')
    },
  )

  it('gives an archived transcript the review page, not the session page', () => {
    // /archived/:id reuses the `:id` param name on purpose (it is what makes
    // PageHost's section gate load-bearing). If the two ever collapsed into one
    // route, an archived transcript would open a live session.
    renderAt('/archived/abc')
    expect(screen.getByTestId('page-review')).toHaveTextContent('abc')
    expect(screen.queryByTestId('page-session-detail')).not.toBeInTheDocument()
  })
})

// ── the drift guard ──────────────────────────────────────────────────────────

describe('route table — every nav destination exists', () => {
  it.each(NAV_DEFS.map((d) => [d.id, d.path] as const))(
    'the %s rail item (%s) resolves to its own section page',
    (id, path) => {
      renderAt(path)
      const landed = url() ?? ''
      // Not "did not redirect": /settings redirects to /settings/general and is
      // perfectly healthy. The property is that the rail item lands inside its
      // OWN section and renders that section's page — a 404 falling through to
      // the catch-all lands in `sessions` and fails both halves.
      expect(navDefForPath(landed.split('?')[0])?.id).toBe(id)
      expect(screen.getByTestId(`page-${id}`)).toBeInTheDocument()
    },
  )
})

// ── the 44-cell matrix: every route × every layout mode ──────────────────────
//
// Umbrella §9: "Every route in §6 renders the same page at 390, 820, 1440 and
// `?kiosk=1`." This is the jsdom half — 11 non-redirect paths × 4 layout modes.
// (The Playwright half, which asserts it at real pixel widths, is PR 10.)
//
// ## What each cell asserts, and why it is not one assertion
//
// "The page renders" is the most vacuity-prone claim in this plan, because three
// different failures all satisfy a naive version of it:
//
//   - an ErrorBoundary catches and renders its fallback — the route "rendered";
//   - the catch-all redirect swallows the path — a page rendered, the wrong one;
//   - the shell renders nothing at all in one mode — no wrong page is shown.
//
// So a cell asserts five things, and each one kills a different failure:
//
//   1. the page's OWN testid is in the document (not "some page", not "no page");
//   2. neither error surface is showing — `ErrorBoundary`'s fallback, or the data
//      router's default error element;
//   3. the URL is still the path asked for (no redirect swallowed it);
//   4. `app.main` is mounted exactly once (the shell laid out, once);
//   5. the rail shape MATCHES THE MODE. This is the assertion that makes the
//      forty-four-ness real: without it the four modes are four identical
//      renders and `stubMatchMedia` might as well not be called.
//
// Assertion 1 is presence, not visibility, and that is deliberate: at `/sessions`
// on phone and tablet the list owns the band and `PageHost` sets `hidden` on the
// page (§4.1). The page is mounted and correct; it is behind the list. Asserting
// visibility here would encode "phone shows the welcome pane over its own list"
// as the requirement, which is the opposite of the layout.

/** The 11 non-redirect paths, each with the page it must land on. */
const MATRIX_PATHS: ReadonlyArray<readonly [path: string, testid: string]> = [
  ['/sessions', 'page-sessions'],
  ['/sessions/abc', 'page-session-detail'],
  ['/sessions/abc/agents/x', 'page-subagent'],
  ['/pipeline', 'page-pipeline'],
  ['/automations', 'page-automations'],
  ['/initiatives', 'page-initiatives'],
  ['/projects', 'page-projects'],
  ['/insights', 'page-insights'],
  ['/archived', 'page-archived'],
  ['/archived/abc', 'page-review'],
  ['/settings/general', 'page-settings'],
]

/**
 * The route patterns that are redirects rather than pages — the complement of
 * `MATRIX_PATHS` in the table. Hand-listed so a NEW page route added to
 * `routes.tsx` without a matrix row is neither covered nor excused, and the
 * coverage guard below reddens.
 */
const REDIRECT_ROUTE_PATTERNS = new Set([
  '/', '/settings',
  '/session/:sessionId', '/session/:sessionId/subagent/:agentId', '/review/:sessionId',
  '/accounts', '/automation', '/library',
  '*',
])

/**
 * The rail as each mode must render it (Task 8). Phone splits `app.rail` into
 * TopBar + NavBar; tablet and desktop use one left NavRail; kiosk has no rail
 * at all. Asserted per cell so a mode that silently fell back to another mode's
 * layout cannot pass as that mode.
 */
const RAIL_SHAPE: Record<LayoutMode, { region: number; top: number; bottom: number; left: number }> = {
  phone: { region: 2, top: 1, bottom: 1, left: 0 },
  tablet: { region: 1, top: 0, bottom: 0, left: 1 },
  desktop: { region: 1, top: 0, bottom: 0, left: 1 },
  kiosk: { region: 0, top: 0, bottom: 0, left: 0 },
}

function renderShellAt(path: string, mode: LayoutMode) {
  stubMatchMedia(LAYOUT_MODE_QUERIES[mode])
  // `readKioskConfig()` reads `window.location.search`, not the router's — a
  // wall display is configured by the URL it was launched with.
  if (mode === 'kiosk') window.history.replaceState(null, '', '/?kiosk=1')

  const router = createMemoryRouter(buildRoutes(<AppShell />), { initialEntries: [path] })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <ThemeProvider>
        {/* The app's real boundary, in the position `main.tsx` puts it: above
            `RouterProvider`. Mounted rather than stubbed so assertion 2 checks
            the production arrangement — including the fact, verified by this
            task's break, that it is NOT what catches a crash inside the shell. */}
        <ErrorBoundary>
          <RouterProvider router={router} />
        </ErrorBoundary>
      </ThemeProvider>
    </QueryClientProvider>,
  )
  return router
}

const count = (sel: string) => document.querySelectorAll(sel).length

describe('every route renders in every layout mode', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
  })

  afterEach(() => {
    window.history.replaceState(null, '', '/')
  })

  it('the matrix covers exactly the non-redirect routes in the table', () => {
    const table = buildRoutes(<TestLayout />)
    const declared = table[0].children![0].children!.map((r) => r.path!)
    const pageRoutes = declared.filter((p) => !REDIRECT_ROUTE_PATTERNS.has(p))
    const covered = MATRIX_PATHS.map(([p]) => {
      const matched = matchRoutes(table, p)
      // A matrix row for a path the table does not declare is a broken row, not
      // a passing one.
      expect(matched, p).not.toBeNull()
      return matched![matched!.length - 1].route.path!
    })

    expect([...new Set(covered)].sort()).toEqual([...pageRoutes].sort())
    expect(pageRoutes).toHaveLength(11)
    expect(MATRIX_PATHS).toHaveLength(11)
  })

  const CELLS = LAYOUT_MODES.flatMap((mode) =>
    MATRIX_PATHS.map(([path, testid]) => [mode, path, testid] as const),
  )

  it('the matrix is 44 cells', () => {
    expect(CELLS).toHaveLength(44)
  })

  it.each(CELLS)('%s · %s renders %s', async (mode, path, testid) => {
    const router = renderShellAt(path, mode)

    // 1 and 2, in one `waitFor` and in THIS order on purpose.
    //
    // BOTH error surfaces are asserted because they are not interchangeable, and
    // the break that proved this matrix showed which one actually fires: a throw
    // inside `AppShellInner` was caught by the DATA ROUTER's default error
    // element, not by `ErrorBoundary` — react-router owns errors thrown anywhere
    // in its own route subtree, and the app's boundary (which sits above
    // `RouterProvider`, exactly as in `main.tsx`) never saw it. A matrix that
    // checked only `ErrorBoundary` would have gone green on a shell that crashed
    // in every kiosk cell.
    //
    // Either surface replaces the tree it caught, so a crash also makes the page
    // testid missing. Checked first, the failure reads "expected <h2/> to be
    // null" against the named surface rather than as a page that timed out for
    // no stated reason.
    await waitFor(() => {
      expect(screen.queryByText('Something went wrong.'), `${mode} ${path} — boundary`).toBeNull()
      expect(screen.queryByText(/Unexpected Application Error/i), `${mode} ${path} — router`).toBeNull()
      expect(screen.queryByTestId(testid), `${mode} ${path} — page`).not.toBeNull()
    })

    // 3. no redirect swallowed the path.
    const { pathname, search } = router.state.location
    expect(pathname + search, `${mode} ${path}`).toBe(path)

    // 4. the shell laid the page out, once.
    expect(count('[data-region="app.main"]'), `${mode} ${path}`).toBe(1)

    // 5. and it laid it out in THIS mode.
    const rail = RAIL_SHAPE[mode]
    expect(count('[data-region="app.rail"]'), `${mode} rail regions`).toBe(rail.region)
    expect(count('[data-rail="top"]'), `${mode} top rail`).toBe(rail.top)
    expect(count('[data-rail="bottom"]'), `${mode} bottom rail`).toBe(rail.bottom)
    expect(count('[data-rail="left"]'), `${mode} left rail`).toBe(rail.left)
  })
})
