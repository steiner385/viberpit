// What the routed Delivery page shows for each address. The two detail bodies
// are tested with their own components (`automation/AutomationDetail.test.tsx`,
// `ci/CiRepoDetail.test.tsx`); this file is only about which one the page picks,
// what its header says, and that an address the list does not contain says so.
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { AUTOMATIONS } from './__fixtures__/automations'
import { DeliveryPage } from './DeliveryPage'
import { AUTOMATION_ROUTE, CI_REPO_ROUTE, DELIVERY_PATH } from './deliveryNavState'

const patch = vi.fn()

// The page's own hooks plus everything the two detail bodies reach for — the
// same set `AutomationDetail.test.tsx` mocks, so the real panes render.
vi.mock('@/api/hooks', () => ({
  useAutomations: () => ({ data: AUTOMATIONS, isLoading: false, isError: false }),
  useStageModels: () => ({
    data: { 'o/r': { repo: 'o/r', nodes: [] } }, isLoading: false, isError: false,
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
vi.mock('@/components/ConsolidatePanel', () => ({ ConsolidatePanel: () => <div data-testid="consolidate" /> }))
vi.mock('@/components/BacklogGroupsPanel', () => ({ BacklogGroupsPanel: () => <div data-testid="groups" /> }))
vi.mock('@/components/ManualDispatchDialog', () => ({ ManualDispatchDialog: () => null }))

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
    expect(screen.getByRole('combobox', { name: 'Mode' })).toHaveValue('auto')
    expect(screen.getByRole('switch', { name: 'PR doctor enabled' })).toBeInTheDocument()
    // The status line and the mode description, as visible header text.
    expect(screen.getByText('2 PRs failing')).toBeInTheDocument()
    expect(screen.getByText(/Acts automatically without confirmation/)).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Status', selected: true })).toBeInTheDocument()
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
})
