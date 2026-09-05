/**
 * App routing.
 *
 * ## What changed at the cutover
 *
 * `App` is now `RouterProvider` and nothing else, so `<App/>` cannot be rendered
 * inside a `MemoryRouter` any more — two routers is an invariant violation. The
 * file mounts what `App` composes instead: `buildRoutes(<AppShell/>)` under a
 * memory router, through `test/renderApp`. Same tree, same route table, same
 * shell; only the history implementation differs, which is exactly what a test
 * should be swapping.
 *
 * The URL is read from the ROUTER (`app.path()`) rather than from a
 * `LocationProbe` component. The probe had to live outside the app tree to avoid
 * a second `useLocation`, which meant it reported the MemoryRouter's location
 * rather than the app's — harmless before, meaningless now that the app owns its
 * own router.
 *
 * ## What the layout mode means here
 *
 * Every case says which mode it means. Routing is mode-independent by design
 * (that is the point of PR 3), so most run in the default phone mode; the two
 * that are ABOUT the layout ask for desktop explicitly.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { Session, Account } from './api/types'
import { anthropicUsage, session } from '@/test/factories'
import { api } from './api/client'
import { renderApp } from './test/renderApp'

vi.mock('./ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./ws/client')>()
  return {
    ...actual,
    useSessionSocket: () => ({
      attach: vi.fn(),
      detach: vi.fn(),
      interrupt: vi.fn(),
      prompt: vi.fn(),
      sendInterrupt: vi.fn(),
      permissionResponse: vi.fn(),
      answerQuestion: vi.fn(),
      setPermissionMode: vi.fn(),
      on: vi.fn(),
      off: vi.fn(),
      connectionState: 'connected' as const,
      socket: null,
    }),
  }
})

const MOCK_SESSIONS: Session[] = [
  session({
    id: 'sess-1',
    name: 'Work',
    accountName: 'alice',
    status: 'idle',
    createdAt: '2026-06-19T10:00:00Z',
    updatedAt: '2026-06-19T10:00:00Z',
  }),
  session({
    id: 'sess-2',
    name: 'Research',
    accountName: 'alice',
    status: 'idle',
    createdAt: '2026-06-19T11:00:00Z',
    updatedAt: '2026-06-19T11:00:00Z',
  }),
]

const MOCK_ACCOUNTS: Account[] = [
  {
    name: 'alice',
    status: 'ok',
    usage: anthropicUsage(20, 30),
    liveSessions: { active: 0, idle: 0 },
    rank: null,
    rankReason: null,
  },
]

vi.mock('./api/client', () => ({
  ApiRequestError: class extends Error {
    status: number; body: Record<string, unknown>
    constructor(status: number, body: Record<string, unknown>) {
      super(`API error ${status}`)
      this.status = status
      this.body = body
    }
  },
  api: {
    get: vi.fn().mockImplementation((path: string) => {
      // Exact path + query only: `/sessions/archived` is a DIFFERENT endpoint,
      // and answering it with the live list makes every session look archived —
      // which sends `useArchiveAdvance` into an endless advance loop.
      if (path === '/sessions' || path.startsWith('/sessions?')) return Promise.resolve(MOCK_SESSIONS)
      if (path === '/accounts') return Promise.resolve(MOCK_ACCOUNTS)
      return Promise.resolve([])
    }),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    del: vi.fn().mockResolvedValue(undefined),
  },
}))

/** The shell's own chrome — present on every route, in every mode. */
const shellIsUp = () => screen.findByRole('navigation', { name: /sections/i })

describe('App routing', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
  })

  it('renders the shell at /sessions', async () => {
    renderApp({ path: '/sessions' })
    await shellIsUp()
    // `Dashboard`'s `<h1>ccswitch</h1>` is gone: `TopBar` renders the brand as
    // plain text on purpose, so the page's own `PageHeader` owns the only `h1`
    // (PhoneNav.test.tsx "renders no h1"). The brand itself is still the
    // top-left of the phone shell.
    expect(screen.getByText('ccswitch')).toBeInTheDocument()
  })

  it('redirects / to /sessions', async () => {
    const app = renderApp({ path: '/' })
    await shellIsUp()
    expect(app.path()).toBe('/sessions')
  })

  it('redirects an unknown path to /sessions', async () => {
    const app = renderApp({ path: '/nonsense/path' })
    await shellIsUp()
    expect(app.path()).toBe('/sessions')
  })

  it('deep-links to a session view at the legacy /session/:id', async () => {
    const app = renderApp({ path: '/session/sess-1' })
    // The legacy singular spelling redirects to the canonical plural one …
    await waitFor(() => expect(app.path()).toBe('/sessions/sess-1'))
    // … and lands on the session pane, not merely on the URL.
    expect(await screen.findByRole('button', { name: /back to session list/i })).toBeInTheDocument()
  })

  it('deep-links to the archived review view at the legacy /review/:id', async () => {
    const app = renderApp({ path: '/review/sess-1' })
    await waitFor(() => expect(app.path()).toBe('/archived/sess-1'))
    expect(await screen.findByRole('button', { name: /back to dashboard/i })).toBeInTheDocument()
  })

  it('redirects /sessions/:id to /archived/:id for an archived session', async () => {
    // An archived session has no live driver, so the live-attach route 404s. The
    // route must redirect it to the read-only review view instead (safety net for
    // links — e.g. automation activity-log entries — that carry no status).
    vi.mocked(api.get).mockImplementation((path: string) => {
      if (path === '/sessions/archived') return Promise.resolve([
        { id: 'arch-1', name: 'Implement issue 16', account: 'alice', cwd: '/tmp/x',
          engineSessionId: 'c-1', permissionMode: 'default',
          createdAt: '2026-06-01T10:00:00Z', archivedAt: '2026-06-02T10:00:00Z' },
      ])
      // Exact path + query only: `/sessions/archived` is a DIFFERENT endpoint,
      // and answering it with the live list makes every session look archived —
      // which sends `useArchiveAdvance` into an endless advance loop.
      if (path === '/sessions' || path.startsWith('/sessions?')) return Promise.resolve(MOCK_SESSIONS)
      if (path === '/accounts') return Promise.resolve(MOCK_ACCOUNTS)
      return Promise.resolve([])
    })
    const app = renderApp({ path: '/sessions/arch-1' })
    await waitFor(() => expect(app.path()).toBe('/archived/arch-1'))
    // …and lands on the read-only view, not on the Archived list: /archived/:id
    // has to out-rank the /archived tab route, or the redirect silently drops
    // the user on a list with no transcript.
    expect(await screen.findByRole('button', { name: /back to dashboard/i })).toBeInTheDocument()
  })

  it('opens a session from the list and updates the URL', async () => {
    const user = userEvent.setup()
    const app = renderApp({ path: '/sessions' })
    await user.click(await screen.findByLabelText(/open session work/i))
    expect(app.path()).toBe('/sessions/sess-1')
    expect(screen.getByRole('button', { name: /back to session list/i })).toBeInTheDocument()
  })

  it('navigates back to /sessions from a session', async () => {
    const user = userEvent.setup()
    const app = renderApp({ path: '/sessions/sess-1' })
    await user.click(await screen.findByRole('button', { name: /back to session list/i }))
    expect(app.path()).toBe('/sessions')
    expect(screen.getByText('ccswitch')).toBeInTheDocument()
  })

  it('redirects legacy /accounts to the Settings accounts section', async () => {
    const app = renderApp({ path: '/accounts' })
    await waitFor(() => expect(app.path()).toBe('/settings/accounts'))
    // The redirect carries the section, so Accounts is what is on screen. This
    // is the default (phone) mode, where the section IS the page — the grouped
    // nav lives on `/settings` itself, so the section landmark is what names it.
    expect(await screen.findByRole('region', { name: 'Accounts' })).toBeInTheDocument()
  })

  it('clicking the Automations rail item updates the URL to /automations', async () => {
    const user = userEvent.setup()
    const app = renderApp({ path: '/sessions', mode: 'desktop' })
    await user.click(await screen.findByRole('link', { name: /^Automations/ }))
    expect(app.path()).toBe('/automations')
  })

  it('clicking the Settings rail item updates the URL to /settings', async () => {
    const user = userEvent.setup()
    const app = renderApp({ path: '/sessions', mode: 'desktop' })
    await user.click(await screen.findByRole('link', { name: /^Settings/ }))
    await waitFor(() => expect(app.path()).toBe('/settings/general'))
  })

  it('navigates to the next session via the › chevron', async () => {
    const user = userEvent.setup()
    const app = renderApp({ path: '/sessions/sess-1' })
    // Wait for useSessions to populate so the chevrons appear.
    await user.click(await screen.findByRole('button', { name: /next session/i }))
    expect(app.path()).toBe('/sessions/sess-2')
  })

  it('shows the list beside the open session at desktop, and instead of it on phone', async () => {
    // The DesktopShell/Dashboard fork's one observable difference, now a
    // property of the region matrix rather than of which component rendered.
    const desktop = renderApp({ path: '/sessions/sess-1', mode: 'desktop' })
    await screen.findByRole('button', { name: /back to session list/i })
    expect(document.querySelector('[data-region="app.list"]')).not.toBeNull()
    desktop.unmount()

    renderApp({ path: '/sessions/sess-1', mode: 'phone' })
    await screen.findByRole('button', { name: /back to session list/i })
    expect(document.querySelector('[data-region="app.list"]')).toBeNull()
  })
})

describe('App deploy overlay', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
  })

  it('mounts the overlay at the root while a deploy runs', async () => {
    vi.mocked(api.get).mockImplementation((path: string) => {
      if (path === '/deploy/status') return Promise.resolve({
        pending: { behind: true, liveSha: 'x', targetSha: 'abcdef0', commits: [], ci: 'green' },
        deploy: { running: true, phases: { update: 'running' }, error: null, warnings: [], targetSha: 'abcdef0' },
      })
      // Exact path + query only: `/sessions/archived` is a DIFFERENT endpoint,
      // and answering it with the live list makes every session look archived —
      // which sends `useArchiveAdvance` into an endless advance loop.
      if (path === '/sessions' || path.startsWith('/sessions?')) return Promise.resolve(MOCK_SESSIONS)
      if (path === '/accounts') return Promise.resolve(MOCK_ACCOUNTS)
      return Promise.resolve([])
    })
    renderApp({ path: '/sessions' })
    expect(await screen.findByText(/upgrading ccswitch/i)).toBeInTheDocument()
  })
})

describe('App session advance', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.del).mockResolvedValue(undefined)
    localStorage.clear()
  })

  it('archiving the open session advances to the neighbour route', async () => {
    const user = userEvent.setup()
    const app = renderApp({ path: '/sessions/sess-1' })
    await user.click(await screen.findByRole('button', { name: /archive session/i }))
    await waitFor(() => expect(app.path()).toBe('/sessions/sess-2'))
  })

  it("archiving advances even when the open session's group is collapsed", async () => {
    // Both mock sessions are idle → the 'your-turn' ("Done") status group. With
    // that group collapsed, the open session is absent from the VISIBLE ordering
    // — the advance must fall back to the full ordering, not bail to /sessions.
    localStorage.setItem('ccswitch.sessionOrder', JSON.stringify({ groupBy: 'status', sortBy: 'status' }))
    localStorage.setItem('ccswitch.groupCollapse', JSON.stringify(['your-turn']))
    const user = userEvent.setup()
    const app = renderApp({ path: '/sessions/sess-1' })
    await user.click(await screen.findByRole('button', { name: /archive session/i }))
    await waitFor(() => expect(app.path()).toBe('/sessions/sess-2'))
  })

  it('the Back button still returns to the list (no advance)', async () => {
    const user = userEvent.setup()
    const app = renderApp({ path: '/sessions/sess-1' })
    await user.click(await screen.findByRole('button', { name: /back to session list/i }))
    await waitFor(() => expect(app.path()).toBe('/sessions'))
  })
})
