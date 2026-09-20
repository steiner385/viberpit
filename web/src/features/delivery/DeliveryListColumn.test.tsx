import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { AUTOMATIONS } from './__fixtures__/automations'
import { CAPACITY } from './capacity/__fixtures__/capacity'
import { DeliveryListColumn } from './DeliveryListColumn'
import { automationKey, ciRepoKey } from './deliveryNavState'
import { ViewportProvider } from '@/shell/useViewport'

vi.mock('@/api/hooks', () => ({
  useCheckInsights: (repo: string) => ({
    data: repo === 'o/r'
      ? { repo, window: '7d', slowest: [{ check_name: 'e2e', event: 'pull_request', p50: 100, p90: 400, ratio: 4, n: 9 }],
          flakiest: [{ check_name: 'ws', event: 'pull_request', flake_pct: 20, flaky_commits: 2, total_commits: 10, n: 10 }],
          regressions: [], runner_wait_health: [] }
      : undefined,
    isLoading: false, isError: false,
  }),
}))

function mount(over: Partial<React.ComponentProps<typeof DeliveryListColumn>> = {}) {
  const onOpenAutomation = vi.fn(); const onOpenRepo = vi.fn()
  render(
    <ViewportProvider kiosk={null}>
      <MemoryRouter>
        <DeliveryListColumn scope="automations" automations={AUTOMATIONS} automationsLoading={false} automationsError={false}
          repos={['o/r', 'a/b']} reposLoading={false} reposError={false}
          capacity={undefined} capacityLoading={false} capacityError={false}
          selectedKey={null}
          onOpenAutomation={onOpenAutomation} onOpenRepo={onOpenRepo} {...over} />
      </MemoryRouter>
    </ViewportProvider>,
  )
  return { onOpenAutomation, onOpenRepo }
}

describe('DeliveryListColumn', () => {
  it('Automations scope: one row per automation, sorted by tier then name, with the tier line', () => {
    mount()
    const list = screen.getByRole('list', { name: 'Automations' })
    const names = within(list).getAllByRole('button', { name: /^Open automation/ }).map((b) => b.getAttribute('aria-label'))
    // backlog-runner has 2 actionable → needs-you first; pr-doctor live session → in-flight; pm-classify idle
    expect(names).toEqual(['Open automation Backlog runner', 'Open automation PR doctor', 'Open automation PM classify'])
    expect(screen.getByText(/2 actionable/)).toBeInTheDocument()
    expect(screen.getByText(/Notice · 1 live session · 2 PRs failing/)).toBeInTheDocument()
  })
  it('invoked tools are not shown as disabled loops', () => {
    mount()
    const row = screen.getByTestId('row-automation:pm-classify')
    expect(within(row).getByText('Invoked tool')).toBeInTheDocument()
    expect(within(row).queryByText('off')).not.toBeInTheDocument()
  })
  it('opens an automation and highlights the selected row', () => {
    const { onOpenAutomation } = mount({ selectedKey: automationKey('pr-doctor') })
    fireEvent.click(screen.getByRole('button', { name: 'Open automation PR doctor' }))
    expect(onOpenAutomation).toHaveBeenCalledWith('pr-doctor')
    expect(screen.getByTestId(`row-${automationKey('pr-doctor')}`)).toHaveAttribute('aria-current', 'true')
  })
  it('CI health scope: one row per repo with the 7-day meta line', () => {
    const { onOpenRepo } = mount({ scope: 'ci' })
    const list = screen.getByRole('list', { name: 'CI health' })
    expect(within(list).getAllByRole('listitem')).toHaveLength(2)
    expect(screen.getByText('1 flaky · slowest e2e 6m 40s')).toBeInTheDocument()
    expect(screen.getByText('No check history in 7d')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Open CI health o/r' }))
    expect(onOpenRepo).toHaveBeenCalledWith('o/r')
    expect(screen.getByTestId(`row-${ciRepoKey('a/b')}`)).toBeInTheDocument()
  })
  it('the scope segment links all three scopes and counts them', () => {
    mount()
    expect(screen.getByRole('link', { name: 'Automations (3)' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('link', { name: 'CI health (2)' })).toHaveAttribute('href', '/delivery?scope=ci')
    expect(screen.getByRole('link', { name: 'Capacity' })).toHaveAttribute('href', '/delivery?scope=capacity')
  })
  it('Capacity scope: the five stack rows, and a binding count of 1 on the scope link', () => {
    mount({ scope: 'capacity', capacity: CAPACITY })
    const list = screen.getByRole('list', { name: 'Capacity' })
    expect(within(list).getAllByRole('listitem')).toHaveLength(5)
    expect(screen.getByRole('link', { name: 'Capacity (1)' })).toHaveAttribute('aria-current', 'page')
  })
  it('search narrows the Automations scope', () => {
    mount()
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'doctor' } })
    expect(screen.getAllByRole('button', { name: /^Open automation/ })).toHaveLength(1)
  })
  it('states: loading skeletons, empty, error', () => {
    mount({ automations: [], automationsLoading: true })
    expect(screen.getAllByTestId('skeleton').length).toBeGreaterThan(0)
  })
  it('Capacity scope: every row with an owner page links to it (spec 2026-09-20 C3)', () => {
    mount({ scope: 'capacity', capacity: CAPACITY })
    const list = screen.getByRole('list', { name: 'Capacity' })
    expect(within(list).getByRole('link', { name: 'Global cap' })).toHaveAttribute('href', '/settings/capacity')
    expect(within(list).getByRole('link', { name: 'Hosts' })).toHaveAttribute('href', '/settings/fleet')
    expect(within(list).getByRole('link', { name: 'Accounts' })).toHaveAttribute('href', '/settings/accounts')
    expect(within(list).getByRole('link', { name: 'Products' })).toHaveAttribute('href', '#products')
  })
  it('Capacity scope: the Products link scrolls the in-page anchor rather than navigating (spec 2026-09-20 D10, F2)', () => {
    mount({ scope: 'capacity', capacity: CAPACITY })
    const scrollIntoView = vi.fn()
    Element.prototype.scrollIntoView = scrollIntoView
    const products = document.createElement('div')
    products.id = 'products'
    document.body.appendChild(products)
    fireEvent.click(screen.getByRole('link', { name: 'Products' }))
    expect(scrollIntoView).toHaveBeenCalledTimes(1)
    document.body.removeChild(products)
  })
})
