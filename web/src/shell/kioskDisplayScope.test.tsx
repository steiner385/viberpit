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
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { act, render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider, type RouteObject } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import type { Session } from '@/api/types'
import type { PrView } from '@/pipeline/types'

let me: unknown = null

const SESSIONS: Session[] = [{
  id: 'a', name: 'First', accountName: 'alice', engine: 'claude_code', engineSessionId: null,
  status: 'idle', createdAt: '2026-06-19T10:00:00Z', prs: [],
}]

// The REAL api client runs; only `fetch` is faked, so the request gate (#3544) is in play and
// the fetch log below is the shell's actual wire traffic.
const fetched: string[] = []
function respond(path: string): unknown {
  if (path === '/sessions' || path === '/sessions?viewing=1') return SESSIONS
  if (path === '/fleet/hosts') return { hosts: [] }
  if (path === '/config') return { adoptionEnabled: false }
  return []
}
async function fakeFetch(url: string, init?: RequestInit): Promise<Response> {
  fetched.push(`${init?.method ?? 'GET'} ${url}`)
  const path = url.replace(/^\/api/, '')
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
  if (path === '/auth/me') {
    const value = me instanceof Promise ? await me : me
    return value instanceof Error ? json({}, 502) : json(value)
  }
  return json(init?.method && init.method !== 'GET' ? {} : respond(path))
}

import { LAYOUT_MODE_QUERIES, MEDIA, stubMatchMedia } from '@/test/matchMedia'
import { regionCounts } from '@/test/regions'
import { ThemeProvider } from '@/shell/theme'
import { buildRoutes } from '@/shell/routes'
import { ApiBaseProvider } from '@/pipeline/ApiBaseContext'
import { PrRow } from '@/pipeline/PrRow'
import { setDisplayReads } from '@/api/displayGate'
import { __resetSocketsForTest, getSessionSocket } from '@/ws/client'
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

/** `DISPLAY_ROUTE_ALLOW`'s GET rows, read from the ONE source (`server/assistant/policy.py`) —
 *  the same read `PipelinePage.test.tsx` does. The server publishes exactly these, as
 *  anchored regex sources, in `GET /api/auth/me` (`display_read_patterns()`). */
function displayReads(): string[] {
  const policy = readFileSync(
    path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../server/assistant/policy.py'), 'utf-8')
  const block = policy.slice(policy.indexOf('DISPLAY_ROUTE_ALLOW'), policy.indexOf('def display_read_patterns'))
  return [...block.matchAll(/\("GET", r"([^"]+)"\)/g)].map((m) => `${m[1]}$`)
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

function renderApp({ scope, kioskParam, meFails = false }: { scope: 'full' | 'display'; kioskParam: boolean; meFails?: boolean }) {
  me = meFails ? new Error('502') : scope === 'display'
    ? { identity: 'display:Wall', scope, authMode: 'display', allowedReads: displayReads() }
    : { identity: 'alice@example.com', scope, authMode: 'tailscale' }
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
  return { router, qc }
}

beforeEach(() => {
  opened.length = 0
  fetched.length = 0
  vi.stubGlobal('fetch', vi.fn(fakeFetch))
  __resetSocketsForTest()
  vi.stubGlobal('WebSocket', CountingWebSocket)
  localStorage.clear()
})

afterEach(() => {
  setDisplayReads(null)
  __resetSocketsForTest()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
  document.documentElement.style.removeProperty('--scale')
})

describe('display scope drives kiosk (AC-G10.8)', () => {
  it('scope "display" without ?kiosk=1: kiosk layout, no Fix, no WebSocket', async () => {
    const { router } = renderApp({ scope: 'display', kioskParam: false })
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
    const { router } = renderApp({ scope: 'full', kioskParam: true })
    await waitFor(() => expect(router.state.location.pathname).toBe('/pipeline'))
    expect(await screen.findByTestId('probe-page')).toHaveAttribute('data-interactive', 'false')
    expect(regionCounts().has('app.rail')).toBe(false)
    expect(screen.getByRole('link', { name: /fix-auth/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Fix' })).toBeNull()
    expect(opened).toEqual([])
  })

  it('neither: the full app — rail, Fix, and the socket opens once the scope is known', async () => {
    const { router } = renderApp({ scope: 'full', kioskParam: false })
    const page = await screen.findByTestId('probe-page')
    expect(router.state.location.pathname).toBe('/sessions')
    expect(page).toHaveAttribute('data-interactive', 'true')
    expect(regionCounts().has('app.rail')).toBe(true)
    expect(screen.getByRole('button', { name: 'Fix' })).toBeInTheDocument()
    await waitFor(() => expect(opened.length).toBeGreaterThan(0))
    expect(new Set(opened)).toEqual(new Set(['/ws']))
  })

  // A failed `/auth/me` is "known" (falls back to the full app) — and must STAY
  // known. TanStack resets a data-less errored query to status 'pending' on every
  // refetch (a later observer mounting, the interval, window focus); keying the
  // hold on `isPending` then destroyed the socket a mounted session view held,
  // so its attach never reached a server again (the e2e rotation-reconnect reds).
  it('a failed /auth/me stays known: a refetch never swaps out the open socket', async () => {
    const { qc } = renderApp({ scope: 'full', kioskParam: false, meFails: true })
    await screen.findByTestId('probe-page')
    await waitFor(() => expect(opened.length).toBeGreaterThan(0))
    const held = getSessionSocket()
    const before = opened.length
    // The refetch is in flight (a real round-trip), so the shell renders the
    // transient 'pending' state before the second failure lands.
    let fail!: (e: Error) => void
    me = new Promise((_, reject) => { fail = reject })
    let refetch!: Promise<void>
    act(() => { refetch = qc.refetchQueries({ queryKey: ['auth', 'me'] }) })
    await waitFor(() => expect(qc.getQueryState(['auth', 'me'])?.fetchStatus).toBe('fetching'))
    expect(getSessionSocket()).toBe(held)
    await act(async () => { fail(new Error('502')); await refetch.catch(() => {}) })
    expect(getSessionSocket()).toBe(held)
    expect(opened.length).toBe(before)
  })

  // #3544: the shell's own list/badge/workspace queries must not poll routes the display
  // allowlist refuses — each is a 403 on every tick. One gate at the request layer, fed by
  // the server's own list (`allowedReads`), so a hook added tomorrow is covered unseen.
  const wirePaths = () => [...new Set(fetched.filter((l) => l.startsWith('GET '))
    .map((l) => new URL(l.slice(4), 'http://edge').pathname))]

  it('a display session polls only allowlisted routes, not the shell\'s own queries', async () => {
    const { qc } = renderApp({ scope: 'display', kioskParam: true })
    await screen.findByTestId('probe-page')
    await waitFor(() => expect(qc.getQueryData(['auth', 'me'])).toBeTruthy())
    // The scope is known from here on; whatever the first paint raced is not the claim.
    fetched.length = 0
    await act(async () => { await qc.refetchQueries({ type: 'all' }) })
    const allow = displayReads().map((src) => new RegExp(`^${src}$`))
    expect(allow.length).toBeGreaterThan(0)
    const paths = wirePaths()
    // Lower bound: the poll really ran (/auth/me is on the list), so an empty log cannot pass.
    expect(paths).toContain('/api/auth/me')
    for (const p of paths) expect(allow.some((rx) => rx.test(p)), `${p} is not in DISPLAY_ROUTE_ALLOW`).toBe(true)
  })

  it('a full session still polls the shell\'s queries (control: the spy can see them)', async () => {
    const { qc } = renderApp({ scope: 'full', kioskParam: false })
    await screen.findByTestId('probe-page')
    await waitFor(() => expect(qc.getQueryData(['auth', 'me'])).toBeTruthy())
    fetched.length = 0
    await act(async () => { await qc.refetchQueries({ type: 'all' }) })
    expect(wirePaths().some((p) => p === '/api/sessions' || p === '/api/fleet/hosts')).toBe(true)
  })
})
