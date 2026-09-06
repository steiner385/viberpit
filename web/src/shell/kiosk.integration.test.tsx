// Kiosk, end to end: `?kiosk=1` renders the real shell, the real route table
// and a real page, and every kiosk rule is asserted on the resulting DOM.
//
// ## Why every case renders the DESKTOP control too
//
// Almost every kiosk assertion is an ABSENCE — no rail, no list, no dock, no
// drawer, no header actions, no dialog, no info banner, no filter buttons. Every
// one of those passes trivially against a shell that threw, a `matchMedia` the
// harness forgot to stub, or a route that never matched. Four of the last eight
// tasks on this project shipped exactly that: a "negative control" that was a
// silent no-op, a break that crashed the route so the error page satisfied
// "renders nothing", a portal that fell back to `<body>`, and a matrix cell an
// override row already covered.
//
// So this file never asserts an absence alone. Each case pairs it with
//   • a positive LOWER BOUND in the same render — `app.main` is present, the
//     page rendered, the danger banner is on screen, the tiles still exist as
//     divs — so a blank document fails; and
//   • the same render at `mode: 'desktop'` with no `?kiosk=1`, where the thing
//     kiosk removes must be PRESENT — so an assertion that could never have
//     found its target fails there instead of passing here.
//
// ## Why a probe page rather than the real Pipeline page
//
// The route table, the shell and every region are real; only the ELEMENT behind
// `/sessions` is swapped, for a page that mounts the three primitives kiosk acts
// on (`PageHeader` actions, `Dialog`, `StatusStrip`). That is the point of the
// test: the provider chain `AppShell → InteractionProvider → PageHost → Outlet →
// page → StatusStrip` is what carries `interactive: false` down, and a page
// stood up outside the shell would not exercise it. Mounting `PipelineTab`
// instead would add a dozen data mocks and test the pipeline's fetching, not the
// shell's read-only contract.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { createMemoryRouter, RouterProvider, type RouteObject } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import type { Session } from '@/api/types'
import type { PrView } from '@/pipeline/types'

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

const SESSIONS: Session[] = [session({ id: 'a', name: 'First' }), session({ id: 'b', name: 'Second' })]

/** One real `PrView`, so at least one `StatusStrip` tile carries a non-zero
 *  count (and, in the interactive control, is an ENABLED button rather than a
 *  disabled one). `stage: 'ci'` buckets to `running` via `bucketPr`. */
const PRS: PrView[] = [
  {
    repo: 'steiner385/cc-account-switcher',
    number: 1234,
    title: 'feat(shell): kiosk',
    url: 'https://github.com/steiner385/cc-account-switcher/pull/1234',
    stage: {
      stage: 'ci', substate: null, percent: 40, etaSeconds: 120,
      etaRangeSeconds: null, overdue: false,
    },
    queueAheadCount: null,
    checks: [],
    groupChecks: null,
    mergeEtaSim: null,
  },
]

// ── the fake daemon (same harness as AppShell.test.tsx) ───────────────────────

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

// The two banner triggers, forced on. Both banners are REAL components declared
// by the real `useAppBanners` with their real tones (`daemon` → danger,
// `input-hold` → info; pinned in useAppBanners.test.tsx); only the hooks that
// decide whether they have anything to say are stubbed.
//
// This matters for the negative half: `update` (info) is gated to
// `import.meta.env.PROD` and can NEVER render under vitest, so "the update
// banner is absent in kiosk" would have been true of every mode — a vacuous
// assertion. `input-hold` is the info banner that genuinely renders here, which
// is why it, not `update`, is the one the desktop control shows present.
vi.mock('@/hooks/useDaemonReachable', () => ({ useDaemonReachable: () => false }))
vi.mock('@/hooks/useInputHold', () => ({
  useInputHold: () => ({ holding: true, count: 2, mode: 'reconnecting' as const }),
}))

import { LAYOUT_MODE_QUERIES, MEDIA, stubMatchMedia } from '@/test/matchMedia'
import { regionCounts, presentAppRegions } from '@/test/regions'
import { ThemeProvider } from '@/shell/theme'
import { buildRoutes } from '@/shell/routes'
import { PageHeader } from '@/ui/PageHeader'
import { Dialog } from '@/ui/Dialog'
import { StatusStrip } from '@/pipeline/StatusStrip'
import type { BannerStackItem } from '@/ui/BannerStack'
import { AppShell, bannersForMode, KIOSK_SCALE } from './AppShell'

/** The visible text each banner renders — queried rather than a test id so the
 *  assertion fails if the banner stops rendering its copy. */
const DANGER_BANNER = 'Back in a moment…'   // DaemonRestartBanner, tone: danger
const INFO_BANNER = 'Reconnecting…'         // UpdatePendingBanner,  tone: info

// ── the probe page ───────────────────────────────────────────────────────────

/**
 * A page built from the three primitives kiosk acts on. Everything above it —
 * the router, the shell, the regions, the providers — is the real app.
 */
function ProbePage() {
  return (
    <div data-testid="probe-page">
      <PageHeader title="Probe" meta="probe meta" actions={<button>New probe</button>} />
      <StatusStrip prs={PRS} activeFilter={null} onFilter={() => {}} />
      <Dialog open onClose={() => {}} title="Probe dialog">
        <button>Probe dialog body</button>
      </Dialog>
    </div>
  )
}

/**
 * Both probed paths, because kiosk REDIRECTS off a section that is not
 * kiosk-safe (`KioskPageGuard`): the harness enters at `/sessions`, which is the
 * most-populated context for the desktop control (a section with a list, in
 * Sessions so the dock applies), and kiosk lands on `/pipeline`, the first
 * configured kiosk page. The same probe page is mounted at both so every kiosk
 * assertion below still has a page to make its absences against — and the
 * redirect itself is asserted on the router's location, not inferred from what
 * rendered.
 */
const PROBED_PATHS = ['/sessions', '/pipeline']

function probeRoutes(): RouteObject[] {
  const [root] = buildRoutes(<AppShell />)
  const layout = root.children![0]
  // `RouteObject` is a union and the INDEX branch types `children` as `undefined`,
  // so `{ ...root/layout, children: … }` is only well-typed once the discriminant
  // is settled. Both of these are the routes that HAVE children; assert it.
  if (root.index) throw new Error('probeRoutes: the root route is an index route')
  if (layout.index) throw new Error('probeRoutes: the layout route is an index route')
  const pages = layout.children!.map((route): RouteObject =>
    route.path !== undefined && PROBED_PATHS.includes(route.path)
      ? { path: route.path, element: <ProbePage /> }
      : route,
  )
  // Every probed path must still exist in the real table — a rename in
  // routes.tsx must redden this file, not silently drop the probe and leave
  // every kiosk absence passing against a catch-all redirect.
  for (const path of PROBED_PATHS) {
    if (!layout.children!.some((r) => r.path === path)) {
      throw new Error(`kiosk.integration: ${path} is no longer a route in routes.tsx`)
    }
  }
  return [{ ...root, children: [{ ...layout, children: pages }] }]
}

// ── harness ──────────────────────────────────────────────────────────────────

/**
 * Render the whole app at `/sessions?drawer=help` — the most-populated context
 * there is (a section WITH a list, in Sessions so the dock applies, with a
 * drawer open). Every region kiosk removes is present in the desktop control
 * because of it. In kiosk the guard redirects that entry to `/pipeline`, which
 * carries the same probe page (see `PROBED_PATHS`).
 */
function renderApp(kiosk: boolean) {
  stubMatchMedia([
    ...LAYOUT_MODE_QUERIES[kiosk ? 'kiosk' : 'desktop'],
    // Push mode, so the desktop control's drawer is an inline column rather
    // than a second focus-trapping overlay stacked on the probe's Dialog.
    MEDIA.drawerPush,
  ])
  // `readKioskConfig()` reads `window.location.search`, not the router's — a
  // wall display is configured by the URL it was launched with.
  window.history.replaceState(null, '', kiosk ? '/?kiosk=1' : '/')

  const router = createMemoryRouter(probeRoutes(), { initialEntries: ['/sessions?drawer=help'] })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return {
    ...render(
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <RouterProvider router={router} />
        </ThemeProvider>
      </QueryClientProvider>,
    ),
    router,
  }
}

const scale = () => document.documentElement.style.getPropertyValue('--scale')

let warn: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
  // The refused dialog warns by contract; capture it rather than let it print.
  warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
})

afterEach(() => {
  warn.mockRestore()
  window.history.replaceState(null, '', '/')
  document.documentElement.style.removeProperty('--scale')
})

// ── the banner filter, as a rule ─────────────────────────────────────────────

describe('bannersForMode — the matrix D cell', () => {
  const item = (id: string, tone: BannerStackItem['tone']): BannerStackItem => ({
    id, priority: 10, tone, node: <p>{id}</p>,
  })
  const ITEMS: BannerStackItem[] = [
    item('d', 'danger'), item('w', 'warning'), item('i', 'info'),
    // A banner declared without a tone — `BannerStackItem` documents it as
    // "treated as 'info'", so kiosk must drop it rather than default it onto a
    // wall display unreviewed.
    { id: 'untoned', priority: 10, node: <p>untoned</p> },
  ]

  it('keeps only danger in kiosk', () => {
    expect(bannersForMode(ITEMS, 'kiosk').map((i) => i.id)).toEqual(['d'])
  })

  it.each(['phone', 'tablet', 'desktop'] as const)('is the identity in %s', (mode) => {
    expect(bannersForMode(ITEMS, mode)).toEqual(ITEMS)
  })
})

// ── the shell at ?kiosk=1 ────────────────────────────────────────────────────

describe('kiosk end to end', () => {
  it('hides the interactive regions and keeps the content ones', async () => {
    const app = renderApp(true)
    await screen.findByTestId('probe-page')

    // The absences …
    for (const id of ['app.rail', 'app.list', 'app.dock', 'app.drawer']) {
      expect(regionCounts().has(id), `${id} must be absent in kiosk`).toBe(false)
    }
    // … and the lower bound that makes them mean something: the shell rendered,
    // the routed page is on screen, and the regions kiosk KEEPS are all there.
    //
    // Scoped to `app.*` (DEC-1): kiosk is a statement about the regions the SHELL
    // places, and a page region (`session.header`, Task 10) is not one of them —
    // the routed page here is a probe with none, but an unscoped equality would
    // break the day a real page rendered under it, for a reason kiosk is not
    // about. Every kiosk-relevant id is still compared by equality.
    expect(presentAppRegions()).toEqual(['app.banners', 'app.main', 'app.overlays'])
    expect(screen.getByTestId('probe-page')).toBeVisible()
    expect(screen.getByRole('heading', { level: 1, name: 'Probe' })).toBeVisible()
    app.unmount()

    // The control: at desktop, with the same path, every one of those regions
    // is present — so the assertions above had something to find.
    renderApp(false)
    await screen.findByTestId('probe-page')
    expect(presentAppRegions()).toEqual(
      ['app.banners', 'app.dock', 'app.drawer', 'app.list', 'app.main', 'app.overlays', 'app.rail'],
    )
  })

  it('sets --scale on <html> and removes it on unmount', async () => {
    expect(scale()).toBe('')

    const app = renderApp(true)
    await screen.findByTestId('probe-page')
    expect(scale()).toBe(KIOSK_SCALE)
    expect(scale()).toBe('1.25')

    app.unmount()
    expect(scale()).toBe('')

    // The control: desktop never sets it, so the assertion above is not just
    // reading a property nothing ever writes.
    const desktop = renderApp(false)
    await screen.findByTestId('probe-page')
    expect(scale()).toBe('')
    desktop.unmount()
  })

  it('shows the danger banner and drops the info one', async () => {
    const app = renderApp(true)
    await screen.findByTestId('probe-page')

    // The lower bound FIRST: the banners region is mounted and the danger
    // banner is genuinely rendering, so "no info banner" is not "no banners".
    expect(regionCounts().get('app.banners')).toBe(1)
    expect(screen.getByText(DANGER_BANNER)).toBeVisible()
    expect(screen.queryByText(INFO_BANNER)).toBeNull()
    app.unmount()

    // The control: desktop shows BOTH, so the info banner is one that really
    // does render here and was filtered out above rather than never mounted.
    renderApp(false)
    await screen.findByTestId('probe-page')
    expect(screen.getByText(DANGER_BANNER)).toBeVisible()
    expect(screen.getByText(INFO_BANNER)).toBeVisible()
  })

  /**
   * The exposure S2 named: a wall display LAUNCHED at `/sessions/abc?kiosk=1` or
   * `/sessions/archived?kiosk=1`. `showInKiosk` gates the `?pages=` attract
   * cycle, which says nothing about the URL somebody types into the display —
   * and in kiosk those pages are strictly worse than in desktop, because the
   * rail is gone (no way to navigate off) and `Dialog` is refused (so the
   * archived scope's Permanent Delete goes silently inert) while Archive
   * (`closeSession.mutate`), Unarchive (`unarchiveSession.mutate`) and a pending
   * permission card stay live for any passer-by.
   */
  it('redirects off a section that is not kiosk-safe, keeping the kiosk params', async () => {
    const app = renderApp(true)
    await screen.findByTestId('probe-page')

    // `/sessions` is not kiosk-safe; `/pipeline` is the first configured page.
    expect(app.router.state.location.pathname).toBe('/pipeline')
    // The search survives, so a reload of the redirected URL is still kiosk.
    expect(app.router.state.location.search).toBe('?drawer=help')
    // Lower bound: the redirect landed on a real, rendered page — not a blank
    // document or the catch-all.
    expect(screen.getByTestId('probe-page')).toBeVisible()
    app.unmount()

    // The control: with no `?kiosk=1` the same entry stays on `/sessions`, so
    // the assertion above is about the guard and not about the route table.
    const desktop = renderApp(false)
    await screen.findByTestId('probe-page')
    expect(desktop.router.state.location.pathname).toBe('/sessions')
  })

  it('refuses a Dialog opened from the page', async () => {
    const app = renderApp(true)
    await screen.findByTestId('probe-page')

    expect(screen.queryByRole('dialog', { name: 'Probe dialog' })).toBeNull()
    expect(screen.queryByText('Probe dialog body')).toBeNull()
    // Positive evidence of a REFUSAL rather than of a dialog that never opened:
    // the primitive logged it, naming the dialog.
    expect(warn).toHaveBeenCalled()
    expect(warn.mock.calls.some((c: unknown[]) => String(c[0]).includes('Probe dialog'))).toBe(true)
    app.unmount()

    // The control: the same `<Dialog open>` in the same page does open at
    // desktop, and nothing is logged.
    warn.mockClear()
    renderApp(false)
    await screen.findByTestId('probe-page')
    expect(screen.getByRole('dialog', { name: 'Probe dialog' })).toBeVisible()
    expect(warn).not.toHaveBeenCalled()
  })

  it('hides page.header actions', async () => {
    const app = renderApp(true)
    await screen.findByTestId('probe-page')

    expect(screen.queryByRole('button', { name: 'New probe' })).toBeNull()
    // The header itself is still on screen with its title and meta.
    expect(screen.getByRole('heading', { level: 1, name: 'Probe' })).toBeVisible()
    expect(screen.getByText('probe meta')).toBeVisible()
    app.unmount()

    renderApp(false)
    await screen.findByTestId('probe-page')
    expect(screen.getByRole('button', { name: 'New probe' })).toBeVisible()
  })

  it('renders StatusStrip tiles as read-only divs, not filter buttons', async () => {
    const app = renderApp(true)
    await screen.findByTestId('probe-page')

    const strip = screen.getByRole('group', { name: 'Status overview' })
    expect(within(strip).queryAllByRole('button')).toHaveLength(0)
    // The lower bound: all six tiles are still on screen — the strip is a
    // glanceable read-only display, not an empty box.
    expect(strip.querySelectorAll('.status-tile')).toHaveLength(6)
    expect(within(strip).getByText('CI running')).toBeVisible()
    app.unmount()

    // The control: the same strip at desktop is six filter buttons.
    renderApp(false)
    await screen.findByTestId('probe-page')
    const live = screen.getByRole('group', { name: 'Status overview' })
    expect(within(live).getAllByRole('button')).toHaveLength(6)
  })
})
