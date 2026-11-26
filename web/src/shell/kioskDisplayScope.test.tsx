// The client follows the server (#1408 FR-G10.8, AC-G10.8): a display-scoped
// session renders as kiosk with `?kiosk=1` stripped from the URL, `?kiosk=1` on
// a full session gets the same layout, and neither case opens `/ws` or renders
// Fix. The real shell and route table run; only the page behind `/sessions` and
// `/pipeline` is a probe (see kiosk.integration.test.tsx for why), and the probe
// renders a real `PrRow` with a fixable session AND an `onFixPr`, so the Fix
// assertion exercises the provider chain rather than a missing prop.
//
// Every absence is paired with the "neither" control, where the same thing is
// present — so a spy that could never see a socket fails there.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider, type RouteObject } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import type { Session } from '@/api/types'
import type { PrView } from '@/pipeline/types'

let me: unknown = null

const SESSIONS: Session[] = [{
  id: 'a', name: 'First', accountName: 'alice', engine: 'claude_code', engineSessionId: null,
  status: 'idle', createdAt: '2026-06-19T10:00:00Z', prs: [],
}]

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      get: vi.fn(async (path: string): Promise<unknown> => {
        if (path === '/auth/me') return me
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

import { LAYOUT_MODE_QUERIES, MEDIA, stubMatchMedia } from '@/test/matchMedia'
import { regionCounts } from '@/test/regions'
import { ThemeProvider } from '@/shell/theme'
import { buildRoutes } from '@/shell/routes'
import { ApiBaseProvider } from '@/pipeline/ApiBaseContext'
import { PrRow } from '@/pipeline/PrRow'
import { __resetSocketsForTest } from '@/ws/client'
import { AppShell } from './AppShell'
import { useInteractive } from './InteractionContext'

const PR: PrView = {
  repo: 'o/r', number: 7, title: 'fix auth', url: 'https://x/pull/7',
  stage: { stage: 'ci', substate: null, percent: null, etaSeconds: null, etaRangeSeconds: null, overdue: false },
  queueAheadCount: null, checks: [], groupChecks: null, mergeEtaSim: null,
  session: { id: 's1', name: 'fix-auth', fixable: true },
}

function ProbePage() {
  const interactive = useInteractive()
  return (
    <div data-testid="probe-page" data-interactive={String(interactive)}>
      <ApiBaseProvider base="/api/pipeline">
        <PrRow pr={PR} hasDeploy={false} queueCulprit={null} onFixPr={() => {}} />
      </ApiBaseProvider>
    </div>
  )
}

const PROBED_PATHS = ['/sessions', '/pipeline']

function probeRoutes(): RouteObject[] {
  const routes = buildRoutes(<AppShell />)
  const root = routes.find((r) => r.path === undefined)!
  const layout = root.children![0]
  if (root.index || layout.index) throw new Error('probeRoutes: unexpected index route')
  const pages = layout.children!.map((route): RouteObject =>
    route.path !== undefined && PROBED_PATHS.includes(route.path)
      ? { path: route.path, element: <ProbePage /> }
      : route,
  )
  for (const path of PROBED_PATHS) {
    if (!layout.children!.some((r) => r.path === path)) throw new Error(`${path} is no longer a route`)
  }
  return [{ ...root, children: [{ ...layout, children: pages }] }]
}

const opened: string[] = []
class CountingWebSocket {
  static readonly OPEN = 1
  readyState = 0
  url: string
  constructor(url: string) { this.url = url; opened.push(url) }
  close() {}
  send() {}
  addEventListener() {}
}

function renderApp({ scope, kioskParam }: { scope: 'full' | 'display'; kioskParam: boolean }) {
  me = { identity: scope === 'display' ? 'display:Wall' : 'alice@example.com', scope, authMode: scope === 'display' ? 'display' : 'tailscale' }
  // A display is an ordinary desktop browser; only the URL or the scope makes it a kiosk.
  stubMatchMedia([...LAYOUT_MODE_QUERIES.desktop, MEDIA.drawerPush])
  window.history.replaceState(null, '', kioskParam ? '/?kiosk=1' : '/')
  const router = createMemoryRouter(probeRoutes(), { initialEntries: ['/sessions'] })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <ThemeProvider>
        <RouterProvider router={router} />
      </ThemeProvider>
    </QueryClientProvider>,
  )
  return router
}

beforeEach(() => {
  opened.length = 0
  __resetSocketsForTest()
  vi.stubGlobal('WebSocket', CountingWebSocket)
  localStorage.clear()
})

afterEach(() => {
  __resetSocketsForTest()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
  document.documentElement.style.removeProperty('--scale')
})

describe('display scope drives kiosk (AC-G10.8)', () => {
  it('scope "display" without ?kiosk=1: kiosk layout, no Fix, no WebSocket', async () => {
    const router = renderApp({ scope: 'display', kioskParam: false })
    // The guard moves a kiosk off Sessions — only once the scope is known.
    await waitFor(() => expect(router.state.location.pathname).toBe('/pipeline'))
    const page = await screen.findByTestId('probe-page')
    expect(page).toHaveAttribute('data-interactive', 'false')
    expect(regionCounts().has('app.rail')).toBe(false)
    expect(regionCounts().has('app.dock')).toBe(false)
    // Lower bound: the row rendered, so Fix's absence is the gate, not a blank row.
    expect(screen.getByRole('link', { name: /fix-auth/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Fix' })).toBeNull()
    expect(opened).toEqual([])
  })

  it('?kiosk=1 with scope "full": same layout, no Fix, no WebSocket', async () => {
    const router = renderApp({ scope: 'full', kioskParam: true })
    await waitFor(() => expect(router.state.location.pathname).toBe('/pipeline'))
    expect(await screen.findByTestId('probe-page')).toHaveAttribute('data-interactive', 'false')
    expect(regionCounts().has('app.rail')).toBe(false)
    expect(screen.getByRole('link', { name: /fix-auth/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Fix' })).toBeNull()
    expect(opened).toEqual([])
  })

  it('neither: the full app — rail, Fix, and the socket opens once the scope is known', async () => {
    const router = renderApp({ scope: 'full', kioskParam: false })
    const page = await screen.findByTestId('probe-page')
    expect(router.state.location.pathname).toBe('/sessions')
    expect(page).toHaveAttribute('data-interactive', 'true')
    expect(regionCounts().has('app.rail')).toBe(true)
    expect(screen.getByRole('button', { name: 'Fix' })).toBeInTheDocument()
    await waitFor(() => expect(opened.length).toBeGreaterThan(0))
    expect(new Set(opened)).toEqual(new Set(['/ws']))
  })
})
