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
import { createMemoryRouter, RouterProvider, useNavigate, useRouteError } from 'react-router-dom'
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
import { PageHost } from './PageHost'

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

function renderAt(pathname: string, opts: RenderOpts = {}) {
  const page = (name: string) => ({
    element: <Page name={name} extra={opts.page} />,
    errorElement: <RouteError />,
  })
  const router = createMemoryRouter(
    [
      {
        element: <Layout dock={opts.dock} sibling={opts.sibling} />,
        errorElement: <RouteError />,
        children: [
          { path: '/sessions', ...page('list') },
          { path: '/sessions/:id', ...page('session') },
          { path: '/sessions/:id/agents/:agentId', ...page('agent') },
          { path: '/review/:id', ...page('review') },
          { path: '/pipeline', ...page('pipeline') },
        ],
      },
    ],
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

  it('mounts no provider for an :id route in another section (/review/:id)', () => {
    // An archived transcript must not attach a live runtime. `:id` alone would.
    const spy = silenceExpectedErrorLog()
    renderAt('/review/a', { page: <RuntimeProbe at="page" /> })
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
