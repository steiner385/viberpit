// What the routed Delivery page shows for each address. The two detail bodies
// are tested with their own components (`automation/AutomationDetail.test.tsx`,
// `ci/CiRepoDetail.test.tsx`); this file is only about which one the page picks,
// what its header says, and that an address the list does not contain says so.
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { act, cleanup, render, screen, fireEvent } from '@testing-library/react'
import { createMemoryRouter, Outlet, RouterProvider, useLocation, useNavigate } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { AutomationStatus } from '@/api/automations'
import { ViewportProvider } from '@/shell/useViewport'
import { LAYOUT_MODE_QUERIES, stubMatchMedia } from '@/test/matchMedia'
import { AUTOMATIONS } from './__fixtures__/automations'
import { CAPACITY } from './capacity/__fixtures__/capacity'
import { DeliveryPage } from './DeliveryPage'
import { AUTOMATION_ROUTE, CI_REPO_ROUTE, DELIVERY_PATH } from './deliveryNavState'

const patch = vi.fn()

// Mutable per-test error flags. Plain `let`s work here because the mock
// factory below is only evaluated once (when `@/api/hooks` is first
// imported), but the arrow functions it returns are re-invoked on every
// render, so a test can flip these before `mount()` and the component sees it.
const hookState: {
  automationsIsError: boolean
  reposIsError: boolean
  status?: AutomationStatus
  backlogRunnerStatus?: AutomationStatus
} = { automationsIsError: false, reposIsError: false }

// The page's own hooks plus everything the two detail bodies reach for — the
// same set `AutomationDetail.test.tsx` mocks, so the real panes render.
vi.mock('@/api/hooks', () => ({
  useAutomations: () => ({
    data: hookState.automationsIsError ? undefined : AUTOMATIONS.map((a) => {
      if (a.id === 'pr-doctor' && hookState.status) return { ...a, status: hookState.status }
      if (a.id === 'backlog-runner' && hookState.backlogRunnerStatus) return { ...a, status: hookState.backlogRunnerStatus }
      return a
    }),
    isLoading: false,
    isError: hookState.automationsIsError,
  }),
  useStageModels: () => ({
    data: hookState.reposIsError ? undefined : { 'o/r': { repo: 'o/r', nodes: [] } },
    isLoading: false,
    isError: hookState.reposIsError,
  }),
  useCheckInsights: () => ({
    data: {
      repo: 'o/r', window: '7d', slowest: [], flakiest: [], regressions: [], runner_wait_health: [],
    },
    isLoading: false,
    isError: false,
  }),
  usePatchAutomation: () => ({ mutate: patch, isPending: false }),
  useAutomationEvents: () => ({ data: [] }),
  useAutomationSessions: () => ({ data: [] }),
  useAutomationActionable: () => ({ data: [] }),
  useAutomationTrigger: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useGithubRepos: () => ({ data: [], isLoading: false }),
  useConsolidation: () => ({ data: undefined }),
  useConsolidate: () => ({ mutate: vi.fn(), isPending: false }),
  // CapacityDetail's own reads — see CapacityDetail.test.tsx for the detailed
  // behaviour; this file only needs the scope to render without crashing.
  useCapacity: () => ({ data: CAPACITY, isLoading: false, isError: false }),
  useReorderProject: () => ({ mutateAsync: vi.fn().mockResolvedValue({}) }),
  useUpdateProductPolicy: () => ({ mutate: vi.fn() }),
  useProject: () => ({ data: undefined }),
}))
// The CI welcome pane's assistant-gaps table — its states are covered by
// `ci/AssistantGapsTable.test.tsx`; here it only needs to render.
vi.mock('@/api/assistant', () => ({
  useAssistantGaps: () => ({
    data: { days: 30, total: 1, gaps: [{ capability: 'rotate github token', count: 1, lastSeen: '2026-09-01T00:00:00Z' }] },
    isLoading: false,
    isError: false,
  }),
}))
vi.mock('./automation/ConsolidatePanel', () => ({ ConsolidatePanel: () => <div data-testid="consolidate" /> }))
vi.mock('./automation/BacklogGroupsPanel', () => ({ BacklogGroupsPanel: () => <div data-testid="groups" /> }))
// Renders the `open` prop rather than nothing, so a test can tell whether
// `AutomationDetail`'s local `customDispatch` flag survived a swap to a
// different automation and back (regression #1468).
vi.mock('./automation/ManualDispatchDialog', () => ({
  ManualDispatchDialog: ({ open }: { open: boolean }) => <div data-testid="dispatch-dialog" data-open={String(open)} />,
}))

/** The address after the window select rewrites it. */
function Location() {
  const { pathname, search } = useLocation()
  return <div data-testid="location">{pathname}{search}</div>
}

// `CapacityDetail` reads the real `useQueryClient()` (for `qc.getQueryData`/
// `invalidateQueries` — not something `@/api/hooks` can mock) AND uses
// `useSettingsForm`'s `useBlocker` for its pin `SaveBar`, which throws
// outside a DATA router — a plain `<MemoryRouter>` no longer suffices, so
// both harnesses below use `createMemoryRouter`/`RouterProvider` instead.
function qc() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

const DELIVERY_ROUTE_PATHS = [DELIVERY_PATH, AUTOMATION_ROUTE, CI_REPO_ROUTE]

function mount(path: string) {
  const router = createMemoryRouter([
    {
      element: <><Outlet /><Location /></>,
      children: DELIVERY_ROUTE_PATHS.map((p) => ({ path: p, element: <DeliveryPage /> })),
    },
  ], { initialEntries: [path] })
  render(
    <QueryClientProvider client={qc()}>
      <ViewportProvider kiosk={null}>
        <RouterProvider router={router} />
      </ViewportProvider>
    </QueryClientProvider>,
  )
}

/** Two in-page links driving `useNavigate` directly, for tests that need to
 *  move between two detail routes without a real list column (the list lives
 *  outside `DeliveryPage`, in the shell). */
function NavButtons() {
  const navigate = useNavigate()
  return (
    <>
      <button onClick={() => navigate('/delivery/automations/backlog-runner')}>go to backlog runner</button>
      <button onClick={() => navigate('/delivery/automations/pr-doctor?tab=config')}>go to pr doctor config</button>
    </>
  )
}

function mountWithNav(path: string) {
  const router = createMemoryRouter([
    {
      element: <><NavButtons /><Outlet /><Location /></>,
      children: DELIVERY_ROUTE_PATHS.map((p) => ({ path: p, element: <DeliveryPage /> })),
    },
  ], { initialEntries: [path] })
  render(
    <QueryClientProvider client={qc()}>
      <ViewportProvider kiosk={null}>
        <RouterProvider router={router} />
      </ViewportProvider>
    </QueryClientProvider>,
  )
}

beforeEach(() => stubMatchMedia(LAYOUT_MODE_QUERIES.desktop))

afterEach(() => {
  hookState.automationsIsError = false
  hookState.reposIsError = false
  hookState.status = undefined
  hookState.backlogRunnerStatus = undefined
})

const h1 = () => screen.getByRole('heading', { level: 1 })

describe('DeliveryPage', () => {
  it('the automations scope with nothing open invites a pick and counts the list', () => {
    mount('/delivery')
    expect(h1()).toHaveTextContent('Delivery')
    expect(screen.getByText('Pick an automation')).toBeInTheDocument()
    expect(screen.getByText(/3 registered/)).toBeInTheDocument()
  })

  it('the Capacity scope has no welcome pane — it always shows the readout', () => {
    mount('/delivery?scope=capacity')
    expect(h1()).toHaveTextContent('Delivery')
    expect(screen.getByText('Push level')).toBeInTheDocument()
    expect(screen.queryByText('Pick an automation')).not.toBeInTheDocument()
  })

  it('the CI scope keeps the section title and invites a repository pick', () => {
    mount('/delivery?scope=ci')
    // Both welcome scopes are "Delivery": `?scope=ci` is a scope of the section,
    // not a page of its own. Only a detail route retitles.
    expect(h1()).toHaveTextContent('Delivery')
    expect(screen.getByText('Pick a repository')).toBeInTheDocument()
    expect(screen.getByText(/1 with pipeline history/)).toBeInTheDocument()
  })

  it('the CI scope welcome shows the assistant-gaps table', () => {
    mount('/delivery?scope=ci')
    expect(screen.getByRole('table', { name: 'Assistant gaps' })).toBeInTheDocument()
    expect(screen.getByText('rotate github token')).toBeInTheDocument()
  })

  it('the automations scope welcome has no assistant-gaps table', () => {
    mount('/delivery')
    expect(screen.getByText('Pick an automation')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Assistant gaps' })).not.toBeInTheDocument()
  })

  it('an open automation names itself, carries its controls in the header, and shows Overview', () => {
    mount('/delivery/automations/pr-doctor')
    expect(h1()).toHaveTextContent('PR doctor')
    // The header controls (R9): the mode select and the enable switch, beside
    // the name rather than inside the tab panel.
    expect(screen.getByRole('combobox', { name: 'Configured mode' })).toHaveValue('auto')
    expect(screen.getByRole('switch', { name: 'PR doctor enabled' })).toBeInTheDocument()
    // The status line (clamped to two lines, the whole string in its title)
    // and the mode's name in the header; what the mode means is a sentence on
    // the Overview, where a paragraph belongs (2026-09-27 audit).
    expect(screen.getByText('2 PRs failing')).toHaveAttribute('title', '2 PRs failing')
    expect(screen.getByText('Configured mode: auto')).toHaveAttribute('title', 'Repairs eligible PRs within configured permissions.')
    expect(screen.getByText(/auto — Repairs eligible PRs/)).toBeInTheDocument()
    expect(screen.getByText('Execute and finish work')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Backlog runner' })).toHaveAttribute('href', '/delivery/automations/backlog-runner')
    expect(screen.getByRole('tab', { name: 'Overview', selected: true })).toBeInTheDocument()
  })

  it('on a narrow phone the header status is one line and the observed/mode row is shed (2026-10-01 Delivery critique, P1)', () => {
    // The header never scrolls away; at 390px it was 189px over ten lines
    // before the tiles began. Below `narrow:` the status clamps to one line and
    // the third row goes — the mode's name is the header select's value too.
    mount('/delivery/automations/pr-doctor')
    const status = screen.getByText('2 PRs failing')
    expect(status.className).toMatch(/\bline-clamp-2\b/)
    expect(status.className).toMatch(/\bnarrow:line-clamp-1\b/)
    expect(screen.getByText('Configured mode: auto').parentElement!.className).toMatch(/\bnarrow:hidden\b/)
  })

  it('distinguishes failed refresh from its last successful observation', () => {
    hookState.status = { text: 'Status unavailable', level: 'unknown', counts: null,
      observedAt: '2026-09-12T10:00:00Z', lastRefreshFailedAt: '2026-09-12T10:05:00Z' }
    mount('/delivery/automations/pr-doctor')
    expect(screen.getByText(/Last observed/)).toHaveAttribute('title', '2026-09-12T10:00:00Z')
    expect(screen.getByText(/Refresh failed/)).toHaveAttribute('title', '2026-09-12T10:05:00Z')
    expect(screen.getByText(/Status unknown/)).toBeInTheDocument()
  })
  it('ages the last observation even when no new server data arrives', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-12T10:02:00Z'))
    hookState.status = { text: 'Status unavailable', level: 'unknown', counts: null, observedAt: '2026-09-12T10:00:00Z' }
    try {
      mount('/delivery/automations/pr-doctor')
      expect(screen.getByText('Last observed 2m ago')).toBeInTheDocument()
      await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
      expect(screen.getByText('Last observed 3m ago')).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })
  // Findability (spec D9): the runner header names the reason it is idle in
  // plain text already (R9); when that reason is capacity, it should also
  // hand the operator a way to the page that explains and controls it.
  it('links to the capacity page when the runner is waiting on capacity (typed reason)', () => {
    hookState.backlogRunnerStatus = { text: 'Waiting on capacity', level: 'warn', counts: null, waitingReason: 'capacity' }
    mount('/delivery/automations/backlog-runner')
    expect(screen.getByRole('link', { name: 'See capacity' })).toHaveAttribute('href', '/delivery?scope=capacity')
  })

  it('also links when the status text names capacity without a typed waitingReason', () => {
    hookState.backlogRunnerStatus = { text: 'Paused: capacity exhausted', level: 'warn', counts: null }
    mount('/delivery/automations/backlog-runner')
    expect(screen.getByRole('link', { name: 'See capacity' })).toBeInTheDocument()
  })

  it('omits the capacity link when the runner is not waiting on capacity', () => {
    mount('/delivery/automations/backlog-runner')
    expect(screen.queryByRole('link', { name: 'See capacity' })).not.toBeInTheDocument()
  })

  it('never shows the capacity link for a different automation, even one whose status mentions capacity', () => {
    hookState.status = { text: 'Waiting on capacity', level: 'warn', counts: null, waitingReason: 'capacity' }
    mount('/delivery/automations/pr-doctor')
    expect(screen.queryByRole('link', { name: 'See capacity' })).not.toBeInTheDocument()
  })

  it('an automation the list does not contain says so and offers the way back', () => {
    mount('/delivery/automations/nope')
    expect(screen.getByText('Automation not found')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /^Back to delivery$/i })).toHaveAttribute('href', '/delivery')
  })

  it('an open repo names itself and puts the window selector in the header', () => {
    mount('/delivery/ci/o/r?window=30d')
    expect(h1()).toHaveTextContent('o/r')
    const select = screen.getByRole('combobox', { name: 'Window' })
    expect(select).toHaveValue('30d')
    expect(screen.getByText('Slowest / most-variable checks')).toBeInTheDocument()
  })

  it('changing the window rewrites the URL rather than pushing a new page', () => {
    mount('/delivery/ci/o/r?window=30d')
    fireEvent.change(screen.getByRole('combobox', { name: 'Window' }), { target: { value: '7d' } })
    expect(screen.getByTestId('location')).toHaveTextContent('/delivery/ci/o/r?window=7d')
  })

  // Regression (#1468): the router reuses the same `AutomationDetail` instance
  // across a param-only route change (no `key`) — the `<Outlet/>` in
  // `shell/regions/PageHost.tsx` is un-keyed, and React reuses whatever
  // element sits at the same JSX position. `customDispatch` is a `useState`
  // local to `AutomationDetail` itself (not a child), so it is the cleanest
  // witness: opening Backlog runner's dispatch dialog, navigating away to a
  // DIFFERENT automation, then back to Backlog runner should find the dialog
  // closed again. Un-keyed, the flag survives the round trip and the dialog
  // reopens itself on return — the same reuse that lets `ConfigPanel` and
  // `ScheduleEditor`'s local state leak across automations.
  it('switching to a different automation and back does not leak backlog runner’s dispatch-dialog flag onto the return trip', () => {
    mountWithNav('/delivery/automations/backlog-runner?tab=queue')
    fireEvent.click(screen.getByRole('button', { name: 'Custom dispatch…' }))
    expect(screen.getByTestId('dispatch-dialog')).toHaveAttribute('data-open', 'true')

    fireEvent.click(screen.getByText('go to pr doctor config'))
    expect(h1()).toHaveTextContent('PR doctor')

    fireEvent.click(screen.getByText('go to backlog runner'))
    expect(h1()).toHaveTextContent('Backlog runner')
    expect(screen.getByTestId('dispatch-dialog')).toHaveAttribute('data-open', 'false')
  })

  it('an open automation shows an error banner when the list fails to load, rather than an empty pane', () => {
    hookState.automationsIsError = true
    mount('/delivery/automations/pr-doctor')
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load automations.')
  })

  it('the welcome pane says the list will fill back in rather than claiming zero when the fetch failed', () => {
    hookState.automationsIsError = true
    mount('/delivery')
    expect(screen.getByText(/No automations to show/)).toBeInTheDocument()
    expect(screen.queryByText('No automations registered')).not.toBeInTheDocument()
  })

  it('below desktop an open automation gets a Back link to its list scope; desktop and the bare list do not', () => {
    // Review focus 5.
    stubMatchMedia(LAYOUT_MODE_QUERIES.phone)
    mount('/delivery/automations/pr-doctor')
    expect(screen.getByRole('link', { name: 'Back to Automations' })).toHaveAttribute('href', '/delivery')
    cleanup()
    mount('/delivery/ci/o/r')
    expect(screen.getByRole('link', { name: 'Back to CI health' })).toHaveAttribute('href', '/delivery?scope=ci')
    cleanup()
    mount('/delivery')
    expect(screen.queryByRole('link', { name: /^Back to/ })).toBeNull()
    cleanup()
    stubMatchMedia(LAYOUT_MODE_QUERIES.desktop)
    mount('/delivery/automations/pr-doctor')
    expect(screen.queryByRole('link', { name: /^Back to/ })).toBeNull()
  })
})
