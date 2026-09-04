/**
 * `?dialog=` — the URL-addressable dialog contract, end to end.
 *
 * ## What changed, and why the subject moved
 *
 * `App` is now `RouterProvider` and nothing else, so this file cannot render
 * `<App/>` inside a `MemoryRouter` any more (two routers is an invariant
 * violation, not a warning). It mounts what `App` composes instead —
 * `buildRoutes(<AppShell/>)` under a memory router, via `test/renderApp` — which
 * is the same tree the browser gets.
 *
 * ## Why this file is load-bearing (Task 18, Item 0)
 *
 * Between Task 17 and Task 18 the `?dialog=new` param had a WRITER
 * (`GlobalActions`' "New session" link) and no READER: `Dashboard` had been the
 * only thing that rendered `NewSessionDialog`, and it was deleted. The primary
 * action of the app silently did nothing in every layout mode. `NewSessionDialogHost`
 * is the reader, and these are the tests that fail if it ever goes away again —
 * including the mode sweep below, which is what DEC-5 ("in every layout mode")
 * actually means.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { Session, Account } from './api/types'
import { renderApp } from './test/renderApp'
import { LAYOUT_MODES } from './shell/useViewport'

vi.mock('./ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./ws/client')>()
  return {
    ...actual,
    useSessionSocket: () => ({
      attach: vi.fn(), detach: vi.fn(), interrupt: vi.fn(), prompt: vi.fn(),
      sendInterrupt: vi.fn(), permissionResponse: vi.fn(), answerQuestion: vi.fn(),
      setPermissionMode: vi.fn(), on: vi.fn(), off: vi.fn(),
      connectionState: 'connected' as const, socket: null,
    }),
  }
})

const MOCK_ACCOUNTS: Account[] = [
  {
    name: 'alice', status: 'ok',
    usage: { session: { pct: 20, resetsAt: null }, weekly: { pct: 30, resetsAt: null } },
    liveSessions: { active: 0, idle: 0 }, rank: null, rankReason: null,
  },
]
const MOCK_SESSIONS: Session[] = []

vi.mock('./api/client', () => ({
  ApiRequestError: class extends Error {
    status: number; body: Record<string, unknown>
    constructor(status: number, body: Record<string, unknown>) {
      super(`API error ${status}`); this.status = status; this.body = body
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
    post: vi.fn(), put: vi.fn(), patch: vi.fn(), del: vi.fn(),
  },
}))

const newSessionDialog = () => screen.findByRole('dialog', { name: /new session/i })

describe('?dialog=new — the New Session dialog', () => {
  beforeEach(() => { vi.clearAllMocks() })

  // DEC-5. `it.each` over the real mode list rather than three hand-written
  // cases: a mode added to `LAYOUT_MODES` gets covered here without anyone
  // remembering to. Kiosk is excluded by the filter below, not by omission —
  // see its own case.
  it.each(LAYOUT_MODES.filter((m) => m !== 'kiosk'))(
    'opens from ?dialog=new in %s mode',
    async (mode) => {
      renderApp({ path: '/sessions?dialog=new', mode })
      expect(await newSessionDialog()).toBeInTheDocument()
    },
  )

  it('opens on a non-Sessions path too — the param is app-level, not the list\'s', async () => {
    // `/archived` rather than `/pipeline`: the pipeline page opens an
    // `EventSource` on mount, which jsdom does not implement.
    renderApp({ path: '/archived?dialog=new', mode: 'desktop' })
    expect(await newSessionDialog()).toBeInTheDocument()
  })

  it('opens nothing for an unrecognised ?dialog= value', async () => {
    renderApp({ path: '/sessions?dialog=bogus', mode: 'desktop' })
    // Wait for the shell to settle, so "no dialog" is a steady state and not
    // simply the frame before one appears.
    await screen.findByRole('navigation', { name: /sections/i })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('the rail\'s New session action is a link to ?dialog=new, and opens it', async () => {
    const user = userEvent.setup()
    const app = renderApp({ path: '/sessions', mode: 'desktop' })
    await screen.findByRole('navigation', { name: /sections/i })

    const trigger = screen.getByRole('link', { name: /new session/i })
    expect(trigger).toHaveAttribute('href', '/sessions?dialog=new')

    await user.click(trigger)
    expect(await newSessionDialog()).toBeInTheDocument()
    expect(app.search()).toContain('dialog=new')
  })

  it('closing clears the param and adds NO history entry (§4.4 replace)', async () => {
    const user = userEvent.setup()
    const app = renderApp({ path: '/sessions?dialog=new', mode: 'desktop' })
    await newSessionDialog()

    await user.click(screen.getByRole('button', { name: /cancel/i }))
    await waitFor(() => expect(app.search()).not.toContain('dialog=new'))

    // The direct reading of "no history push"…
    expect(app.historyAction()).toBe('REPLACE')
    // …and the user-visible consequence. The stack started with ONE entry, so
    // if the close had pushed, Back would land on `?dialog=new` and reopen the
    // dialog. With replace there is nowhere to go back to.
    await user.keyboard('{Escape}')
    await app.router.navigate(-1)
    await waitFor(() => expect(app.search()).not.toContain('dialog=new'))
    expect(screen.queryByRole('dialog', { name: /new session/i })).not.toBeInTheDocument()
  })

  it('keeps the other search params when it closes', async () => {
    const user = userEvent.setup()
    const app = renderApp({ path: '/sessions?drawer=help&dialog=new', mode: 'desktop' })
    await newSessionDialog()

    await user.click(screen.getByRole('button', { name: /cancel/i }))
    await waitFor(() => expect(app.search()).not.toContain('dialog=new'))
    expect(app.search()).toContain('drawer=help')
  })
})

describe('?dialog=settings — the legacy Settings modal link', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('redirects to the Settings page with the param dropped', async () => {
    const app = renderApp({ path: '/sessions?dialog=settings', mode: 'desktop' })

    await waitFor(() => expect(app.path()).toBe('/settings/general'))
    expect(app.search()).not.toContain('dialog=settings')
    // The real Settings page, with its own section strip — not a modal.
    expect(await screen.findByRole('tablist', { name: /settings sections/i })).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})
