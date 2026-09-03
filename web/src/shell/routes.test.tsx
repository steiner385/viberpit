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
import { describe, it, expect, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { createMemoryRouter, Outlet, RouterProvider, useLocation } from 'react-router-dom'
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

import { NAV_DEFS, navDefForPath } from './navDefs'
import { buildRoutes } from './routes'

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
