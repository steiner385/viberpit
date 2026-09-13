// What the routed Delivery page shows for each address. The two detail bodies
// are tested with their own components (`automation/AutomationDetail.test.tsx`,
// `ci/CiRepoDetail.test.tsx`); this file is only about which one the page picks,
// what its header says, and that an address the list does not contain says so.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import type { AutomationStatus } from '@/api/automations'
import { AUTOMATIONS } from './__fixtures__/automations'
import { DeliveryPage } from './DeliveryPage'
import { AUTOMATION_ROUTE, CI_REPO_ROUTE, DELIVERY_PATH } from './deliveryNavState'

const patch = vi.fn()

// Mutable per-test error flags. Plain `let`s work here because the mock
// factory below is only evaluated once (when `@/api/hooks` is first
// imported), but the arrow functions it returns are re-invoked on every
// render, so a test can flip these before `mount()` and the component sees it.
const hookState: { automationsIsError: boolean; reposIsError: boolean; status?: AutomationStatus } = { automationsIsError: false, reposIsError: false }

// The page's own hooks plus everything the two detail bodies reach for — the
// same set `AutomationDetail.test.tsx` mocks, so the real panes render.
vi.mock('@/api/hooks', () => ({
  useAutomations: () => ({
    data: hookState.automationsIsError ? undefined : AUTOMATIONS.map((a) => a.id === 'pr-doctor' && hookState.status ? { ...a, status: hookState.status } : a),
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

function mount(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        {[DELIVERY_PATH, AUTOMATION_ROUTE, CI_REPO_ROUTE].map((p) => (
          <Route key={p} path={p} element={<DeliveryPage />} />
        ))}
      </Routes>
      <Location />
    </MemoryRouter>,
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
  render(
    <MemoryRouter initialEntries={[path]}>
      <NavButtons />
      <Routes>
        {[DELIVERY_PATH, AUTOMATION_ROUTE, CI_REPO_ROUTE].map((p) => (
          <Route key={p} path={p} element={<DeliveryPage />} />
        ))}
      </Routes>
      <Location />
    </MemoryRouter>,
  )
}

afterEach(() => {
  hookState.automationsIsError = false
  hookState.reposIsError = false
  hookState.status = undefined
})

const h1 = () => screen.getByRole('heading', { level: 1 })

describe('DeliveryPage', () => {
  it('the automations scope with nothing open invites a pick and counts the list', () => {
    mount('/delivery')
    expect(h1()).toHaveTextContent('Delivery')
    expect(screen.getByText('Pick an automation')).toBeInTheDocument()
    expect(screen.getByText(/3 registered/)).toBeInTheDocument()
  })

  it('the CI scope keeps the section title and invites a repository pick', () => {
    mount('/delivery?scope=ci')
    // Both welcome scopes are "Delivery": `?scope=ci` is a scope of the section,
    // not a page of its own. Only a detail route retitles.
    expect(h1()).toHaveTextContent('Delivery')
    expect(screen.getByText('Pick a repository')).toBeInTheDocument()
    expect(screen.getByText(/1 with pipeline history/)).toBeInTheDocument()
  })

  it('an open automation names itself, carries its controls in the header, and shows Status', () => {
    mount('/delivery/automations/pr-doctor')
    expect(h1()).toHaveTextContent('PR doctor')
    // The header controls (R9): the mode select and the enable switch, beside
    // the name rather than inside the tab panel.
    expect(screen.getByRole('combobox', { name: 'Configured mode' })).toHaveValue('auto')
    expect(screen.getByRole('switch', { name: 'PR doctor enabled' })).toBeInTheDocument()
    // The status line and the mode description, as visible header text.
    expect(screen.getByText('2 PRs failing')).toBeInTheDocument()
    expect(screen.getByText(/Configured mode: auto — Repairs eligible PRs/)).toBeInTheDocument()
    expect(screen.getByText('Execute and finish work')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Backlog runner' })).toHaveAttribute('href', '/delivery/automations/backlog-runner')
    expect(screen.getByRole('tab', { name: 'Status', selected: true })).toBeInTheDocument()
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
    mountWithNav('/delivery/automations/backlog-runner')
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
})
