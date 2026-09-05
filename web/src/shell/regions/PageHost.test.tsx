// PageHost — the `app.main` region, and the F2.3 provider-scope fix.
//
// The property this file exists to pin is a BOUNDARY, so it is asserted from
// both sides:
//
//   • INSIDE — a probe rendered through the `<Outlet/>`, and a second probe
//     handed in as `dock`, both read `useSessionRuntimeContext()` without
//     throwing on `/sessions/:id`. The dock half is the reason the `dock` prop
//     exists at all: the composer must share the transcript's runtime.
//
//   • OUTSIDE — the same probe rendered as a SIBLING of `PageHost` throws on
//     the very same route. That is the half `DesktopShell` got wrong: it wrapped
//     the whole shell in `<SessionRuntimeProvider key={selection.id}>`, so
//     selecting a session remounted the session list and reset its scrollTop,
//     which it papered over with a `listScrollTop`/`restoreListScroll`
//     callback-ref pair. `ContextList.test.tsx` asserts the list's DOM node
//     survives a `:id` change; this file asserts why it can — the provider
//     cannot reach it.
//
// The provider itself is NOT mocked. Only `@/ws/client`'s socket is stubbed (the
// pattern `SessionRuntimeProvider.test.tsx` already uses), so
// `useSessionRuntimeContext` is the real hook with the real throw. A mocked
// pass-through provider — the shape `DesktopAdvanceOnArchive.test.tsx` uses, for
// a test that only needs the subtree inert — would make every "throws with no
// provider" assertion below a test of the mock instead of a test of the scope.
//
// Scope note on axe: jsdom applies no stylesheet, so the axe pass here covers
// roles, names and ARIA wiring ONLY. Its `color-contrast` rule always returns
// `incomplete` and `toHaveNoViolations()` inspects `violations` alone — a green
// assertion is NOT evidence of adequate contrast. Contrast is checked separately
// by src/__tests__/contrast.test.ts against the tokens in index.css.
import * as React from 'react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { axe, toHaveNoViolations } from 'jest-axe'
import {
  createMemoryRouter, RouterProvider, useNavigate, useRouteError, type RouteObject,
} from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { Session } from '@/api/types'

expect.extend(toHaveNoViolations)

// Reassigned per test; read at render time, never at mock-factory time.
let SESSIONS: Session[] = []

// Partial mock: `PageHost` and the runtime both read the API layer and jsdom has
// no daemon. Every other hook in the module stays real.
vi.mock('@/api/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/hooks')>()
  return {
    ...actual,
    useSessions: () => ({ data: SESSIONS, isLoading: false }),
    useAccounts: () => ({ data: [] }),
    useSettings: () => ({ data: undefined }),
  }
})

// The websocket is the only genuinely external edge of the REAL provider.
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

import { useSessionRuntimeContext } from '@/components/session/SessionRuntimeProvider'
import { resetDocumentTitle, setBaseTitle } from '@/shell/documentTitle'
import { buildRoutes } from '@/shell/routes'
import { PageHost } from './PageHost'

// ── typed fixtures (R10: real values, never `as unknown as T`) ────────────────

function session(over: Partial<Session> = {}): Session {
  return {
    id: 'a',
    name: 'First',
    accountName: 'alice',
    engine: 'claude_code',
    engineSessionId: null,
    status: 'idle',
    createdAt: '2026-06-19T10:00:00Z',
    prs: [],
    ...over,
  }
}

/** `waiting_input` triages to `needs-you` — see session/triageTier. */
const needsYou = (id: string) => session({ id, name: id, status: 'waiting_input' })

// ── probes ───────────────────────────────────────────────────────────────────

/**
 * Reads the REAL runtime context. Renders the session id it was given and any
 * transcript text, so the same component proves three things: that a provider
 * exists, which session it is for, and whether `initialPrompt` was seeded.
 */
function RuntimeProbe({ at }: { at: string }) {
  const rt = useSessionRuntimeContext()
  const text = rt.state.turns
    .flatMap((t) => t.blocks)
    .flatMap((b) => (b.kind === 'text' ? [b.text] : []))
    .join(' ')
  return <div data-testid={`probe-${at}`}>{`${rt.sessionId}|${text}`}</div>
}

function Page({ name, extra }: { name: string; extra?: React.ReactNode }) {
  return (
    <div data-testid={`page-${name}`}>
      {name}
      {extra}
    </div>
  )
}

/**
 * A `RouterProvider` catches render errors in its own default boundary rather
 * than rethrowing out of `render()`, so "the probe throws" is asserted by
 * surfacing the caught error here instead of with `expect(...).toThrow()`.
 * Declared on both the layout route and its children, so a throw from a sibling
 * region and a throw from the routed page are both visible — and the sibling
 * case still leaves the rest of the layout mounted.
 */
function RouteError() {
  const error = useRouteError()
  return <div data-testid="route-error">{error instanceof Error ? error.message : String(error)}</div>
}

// ── harness ──────────────────────────────────────────────────────────────────

/**
 * Mounts `PageHost` the way `AppShell` will: as the layout route element, with
 * regions rendered beside it. `outside` stands in for `ContextList` — it lives
 * OUTSIDE the region, which is exactly what the provider must not reach.
 */
function Layout({ dock, sibling }: { dock?: React.ReactNode; sibling?: React.ReactNode }) {
  const navigate = useNavigate()
  return (
    <div>
      <div data-testid="outside">
        <button type="button" onClick={() => navigate('/sessions/b')}>go b</button>
        <button type="button" onClick={() => navigate('/sessions')}>go list</button>
        {sibling}
      </div>
      <PageHost dock={dock} />
    </div>
  )
}

interface RenderOpts {
  dock?: React.ReactNode
  sibling?: React.ReactNode
  page?: React.ReactNode
  state?: unknown
}

/**
 * The routes this file exercises, and the probe each one renders instead of its
 * real page.
 *
 * Note `/sessions/archived/:archivedId`: the canonical read-only review route
 * (umbrella §6, DEC-6) shares its SECTION with `/sessions/:id` — both resolve to
 * `sessions` via `navDefForPath` — but does NOT reuse the `:id` param name. The
 * param name, not the section, is what makes the gate load-bearing rather than
 * defensive: `useOpenSessionId` reads `useParams().id`, so the archived route's
 * `archivedId` param is simply invisible to it.
 */
const PROBE_PAGES: Record<string, string> = {
  '/sessions': 'list',
  '/sessions/:id': 'session',
  '/sessions/:id/agents/:agentId': 'agent',
  '/sessions/archived/:archivedId': 'review',
  '/pipeline': 'pipeline',
}

/**
 * Swaps the real page elements for probes, leaving the paths — and the rest of
 * the table, redirects and catch-all included — exactly as `routes.tsx`
 * declares them.
 *
 * This file used to hand-write its own five-route table. That made the section
 * gate agree with an approximation of the app's routing rather than with the
 * app's routing: rename `ARCHIVED_SESSION_ROUTE`'s param in `sessionNavState.ts`
 * and the gate would have gone on passing here while archived transcripts
 * opened live runtimes in the app. The throw below turns that drift into a
 * failure instead.
 */
function probeRoutes(children: RouteObject[], extra: React.ReactNode): RouteObject[] {
  const found = new Set<string>()
  const out = children.map((route): RouteObject => {
    const name = route.path ? PROBE_PAGES[route.path] : undefined
    if (!route.path || !name) return route
    found.add(route.path)
    return {
      path: route.path,
      element: <Page name={name} extra={extra} />,
      errorElement: <RouteError />,
    }
  })
  const missing = Object.keys(PROBE_PAGES).filter((p) => !found.has(p))
  if (missing.length) throw new Error(`routes.tsx no longer declares: ${missing.join(', ')}`)
  return out
}

/**
 * Mounts `PageHost` as the layout route of the REAL route table, the way
 * `AppShell` will.
 */
function harness(opts: RenderOpts): RouteObject[] {
  const [gate] = buildRoutes(<Layout dock={opts.dock} sibling={opts.sibling} />)
  const layout = gate.children?.[0]
  if (!layout?.children) throw new Error('buildRoutes no longer nests a layout route with children')
  return [{
    element: gate.element,
    errorElement: <RouteError />,
    children: [{
      element: layout.element,
      // Declared on the layout route as well as its children, so a throw from a
      // sibling region and a throw from the routed page are both visible.
      errorElement: <RouteError />,
      children: probeRoutes(layout.children, opts.page),
    }],
  }]
}

function renderAt(pathname: string, opts: RenderOpts = {}) {
  const router = createMemoryRouter(
    harness(opts),
    { initialEntries: [opts.state === undefined ? pathname : { pathname, state: opts.state }] },
  )
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

const mainNode = () => document.querySelector('[data-region="app.main"]')

const NO_PROVIDER = 'useSessionRuntimeContext must be used within SessionRuntimeProvider'

/** React and React Router both log a caught render error; that noise is expected. */
function silenceExpectedErrorLog() {
  return vi.spyOn(console, 'error').mockImplementation(() => {})
}

beforeEach(() => {
  SESSIONS = [session({ id: 'a', name: 'First' }), session({ id: 'b', name: 'Second' })]
  resetDocumentTitle()
})

// ── the region itself ────────────────────────────────────────────────────────

describe('PageHost — the app.main region', () => {
  it('marks itself as app.main, exactly once', () => {
    renderAt('/sessions')
    expect(document.querySelectorAll('[data-region="app.main"]')).toHaveLength(1)
  })

  it('renders the routed outlet content inside the region', () => {
    renderAt('/sessions')
    expect(mainNode()).toContainElement(screen.getByTestId('page-list'))
  })

  it('renders the dock inside the region, after the page', () => {
    renderAt('/sessions/a', { dock: <div data-testid="dock">dock</div> })
    const main = mainNode()
    expect(main).toContainElement(screen.getByTestId('dock'))
    expect(
      screen.getByTestId('page-session').compareDocumentPosition(screen.getByTestId('dock'))
      & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
  })

  it('omits the dock when the shell hands it none', () => {
    renderAt('/sessions')
    expect(screen.queryByTestId('dock')).not.toBeInTheDocument()
  })

  it('lets the row-flex parent shrink it — min-w-0, so wide content cannot push the pane off-screen', () => {
    // On desktop `AppShell` lays the list and this region out in a `flex-row`.
    // A flex item's automatic minimum width is its min-content width, so
    // without `min-w-0` any unbreakable descendant (a long token in the
    // transcript, a nowrap meta cell) widens the WHOLE pane past the viewport
    // and the session header's archive button — the last thing on its row —
    // is the first casualty. The old `DesktopShell` main pane got this for free
    // from `overflow-hidden`; PR 3 (#978) dropped both. jsdom does no layout,
    // so the contract is pinned on the class, and `ContextList` carries the
    // same class on its column for the same reason.
    renderAt('/sessions/a')
    expect(mainNode()).toHaveClass('min-w-0')
  })

  it('has no axe violations (roles/names only — jsdom has no stylesheet)', async () => {
    const { container } = renderAt('/sessions/a', { dock: <div>dock</div> })
    expect(await axe(container)).toHaveNoViolations()
  })
})

// ── provider scope: the F2.3 fix ─────────────────────────────────────────────

describe('PageHost — session runtime scope (F2.3)', () => {
  it('provides the runtime to the routed page when a session is open', () => {
    renderAt('/sessions/a', { page: <RuntimeProbe at="page" /> })
    expect(screen.getByTestId('probe-page')).toHaveTextContent('a|')
  })

  it('provides the SAME runtime to the dock, so the composer shares it', () => {
    renderAt('/sessions/a', {
      page: <RuntimeProbe at="page" />,
      dock: <RuntimeProbe at="dock" />,
    })
    expect(screen.getByTestId('probe-dock')).toHaveTextContent('a|')
  })

  it('does NOT reach a sibling region — a probe outside app.main still throws', () => {
    const spy = silenceExpectedErrorLog()
    renderAt('/sessions/a', { sibling: <RuntimeProbe at="outside" /> })
    spy.mockRestore()
    expect(screen.getByTestId('route-error')).toHaveTextContent(NO_PROVIDER)
  })

  it('mounts no provider when no session is open', () => {
    const spy = silenceExpectedErrorLog()
    renderAt('/sessions', { page: <RuntimeProbe at="page" /> })
    spy.mockRestore()
    expect(screen.getByTestId('route-error')).toHaveTextContent(NO_PROVIDER)
  })

  it('keeps the runtime mounted on a sub-agent route, keyed on the session', () => {
    renderAt('/sessions/a/agents/sub-1', { page: <RuntimeProbe at="page" /> })
    expect(screen.getByTestId('probe-page')).toHaveTextContent('a|')
  })

  it('mounts no provider for an archived transcript (/sessions/archived/:archivedId)', () => {
    // An archived transcript must not attach a live runtime — no socket, no
    // transcript reducer, no detach-on-unmount. `/sessions/archived/:archivedId`
    // is the SAME SECTION as `/sessions/:id` (both resolve to `sessions`), so a
    // gate on the section alone would silently attach one here; the param name
    // (`archivedId`, not `id`) is what actually keeps it off.
    const spy = silenceExpectedErrorLog()
    renderAt('/sessions/archived/a', { page: <RuntimeProbe at="page" /> })
    spy.mockRestore()
    expect(screen.getByTestId('route-error')).toHaveTextContent(NO_PROVIDER)
  })
})

// ── what remounts, and what does not ─────────────────────────────────────────

describe('PageHost — remount boundary', () => {
  it('re-keys the runtime when :id changes, so each session gets a fresh one', async () => {
    const user = userEvent.setup()
    renderAt('/sessions/a', { page: <RuntimeProbe at="page" /> })
    expect(screen.getByTestId('probe-page')).toHaveTextContent('a|')

    const before = mainNode()
    await user.click(screen.getByRole('button', { name: 'go b' }))

    expect(screen.getByTestId('probe-page')).toHaveTextContent('b|')
    // A NEW node: the keyed provider deliberately remounts the page and the dock
    // so the transcript reducer starts clean. `toBeInTheDocument()` would pass
    // either way and prove nothing.
    expect(mainNode()).not.toBe(before)
  })

  it('leaves everything outside the region alone across a :id change', async () => {
    const user = userEvent.setup()
    renderAt('/sessions/a')
    const before = screen.getByTestId('outside')

    await user.click(screen.getByRole('button', { name: 'go b' }))

    // The same DOM node, not merely a node that is still present — this is the
    // guarantee `DesktopShell`'s shell-wide provider destroyed, and the reason
    // its `listScrollTop`/`restoreListScroll` workaround existed.
    expect(screen.getByTestId('outside')).toBe(before)
  })

  it('tears the runtime down when the session closes', async () => {
    const user = userEvent.setup()
    renderAt('/sessions/a', { page: <RuntimeProbe at="page" /> })
    expect(screen.getByTestId('probe-page')).toHaveTextContent('a|')

    const spy = silenceExpectedErrorLog()
    await user.click(screen.getByRole('button', { name: 'go list' }))
    spy.mockRestore()

    // The same probe that read the runtime a moment ago now finds no provider —
    // closing a session unmounts it rather than leaving a stale one attached.
    expect(screen.getByTestId('route-error')).toHaveTextContent(NO_PROVIDER)
    // …and the region outside it is untouched by that teardown.
    expect(screen.getByTestId('outside')).toBeInTheDocument()
  })
})

// ── initialPrompt, carried in router state ───────────────────────────────────

describe('PageHost — initialPrompt', () => {
  it('seeds the prompt carried in router state into the transcript', () => {
    renderAt('/sessions/a', {
      page: <RuntimeProbe at="page" />,
      state: { initialPrompt: 'fix the bug' },
    })
    expect(screen.getByTestId('probe-page')).toHaveTextContent('a|fix the bug')
  })

  it('seeds nothing on a plain open (no router state)', () => {
    renderAt('/sessions/a', { page: <RuntimeProbe at="page" /> })
    expect(screen.getByTestId('probe-page')).toHaveTextContent('a|')
  })

  it('ignores router state that carries no prompt', () => {
    renderAt('/sessions/a', { page: <RuntimeProbe at="page" />, state: { from: '/pipeline' } })
    expect(screen.getByTestId('probe-page')).toHaveTextContent('a|')
  })
})

// ── the needs-you count ──────────────────────────────────────────────────────

describe('PageHost — needs-you title', () => {
  it('reports the needs-you count from every route, not just the dashboard', () => {
    SESSIONS = [needsYou('a'), needsYou('b'), session({ id: 'c' })]
    renderAt('/pipeline')
    expect(document.title).toContain('(2)')
  })

  it('contributes the count to the page title instead of clobbering it', () => {
    SESSIONS = [needsYou('a'), needsYou('b')]
    setBaseTitle(Symbol('page'), 'Pipeline')
    renderAt('/pipeline')
    expect(document.title).toBe('(2) Pipeline — ccswitch')
  })

  it('reports no count when nothing needs the user', () => {
    SESSIONS = [session({ id: 'a' }), session({ id: 'b' })]
    renderAt('/sessions')
    expect(document.title).toBe('ccswitch')
  })
})
