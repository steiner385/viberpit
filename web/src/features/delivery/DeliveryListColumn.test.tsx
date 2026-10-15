import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { AUTOMATIONS } from './__fixtures__/automations'
import { CAPACITY } from './capacity/__fixtures__/capacity'
import { DeliveryListColumn } from './DeliveryListColumn'
import { AUTOMATIONS_ID_SCOPE, CI_ID_SCOPE, automationKey, ciRepoKey } from './deliveryNavState'
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
  const { unmount } = render(
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
  return { onOpenAutomation, onOpenRepo, unmount }
}

describe('DeliveryListColumn', () => {
  it('Automations scope: one row per automation, sorted by tier then name, with the tier line', () => {
    mount()
    const list = screen.getByRole('list', { name: 'Automations' })
    const names = within(list).getAllByRole('button', { name: /^Open automation/ }).map((b) => b.getAttribute('aria-label'))
    // backlog-runner has 2 actionable → needs-you first; pr-doctor warn with no
    // typed wait → blocked (2026-10-01 Delivery critique, P2); pm-classify idle
    expect(names).toEqual(['Open automation Backlog runner', 'Open automation PR doctor', 'Open automation PM classify'])
    expect(screen.getByText(/2 actionable/)).toBeInTheDocument()
    expect(screen.getByText(/Blocked · 0 actionable · 1 live session · 2 PRs failing/)).toBeInTheDocument()
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
  // ── the keyboard can enter the list (2026-10-01 Delivery critique, P1) ──
  // Every row used to be `tabindex -1` until one was selected, and nothing is
  // selected on arrival: Tab skipped the whole column. Same contract as the
  // roadmap tree (`ui/list/useRovingRows`): one tab stop, arrows to walk it.
  const control = (name: string) => screen.getByRole('button', { name })

  it('Automations list has ONE tab stop: the first row on arrival, the open row once there is one', () => {
    const { unmount } = mount()
    expect(control('Open automation Backlog runner')).toHaveAttribute('tabindex', '0')
    expect(control('Open automation PR doctor')).toHaveAttribute('tabindex', '-1')
    expect(control('Open automation PM classify')).toHaveAttribute('tabindex', '-1')
    unmount()
    mount({ selectedKey: automationKey('pr-doctor') })
    expect(control('Open automation PR doctor')).toHaveAttribute('tabindex', '0')
    expect(control('Open automation Backlog runner')).toHaveAttribute('tabindex', '-1')
  })

  it('ArrowDown/ArrowUp/Home/End move focus between automation rows, under a namespaced id', () => {
    mount()
    const first = control('Open automation Backlog runner')
    expect(first.id).toBe(`row-${AUTOMATIONS_ID_SCOPE}-backlog-runner`)
    first.focus()
    fireEvent.keyDown(first, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(control('Open automation PR doctor'))
    fireEvent.keyDown(document.activeElement!, { key: 'End' })
    expect(document.activeElement).toBe(control('Open automation PM classify'))
    // The tab stop follows the cursor: Tab away and back lands where you left.
    expect(control('Open automation PM classify')).toHaveAttribute('tabindex', '0')
    expect(first).toHaveAttribute('tabindex', '-1')
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' })  // last row: stays
    expect(document.activeElement).toBe(control('Open automation PM classify'))
    fireEvent.keyDown(document.activeElement!, { key: 'Home' })
    expect(document.activeElement).toBe(first)
    fireEvent.keyDown(first, { key: 'ArrowUp' })  // first row: stays
    expect(document.activeElement).toBe(first)
  })

  it('CI health list has the same roving tab stop and arrows', () => {
    mount({ scope: 'ci' })
    const first = control('Open CI health a/b')
    expect(first).toHaveAttribute('tabindex', '0')
    expect(first.id).toBe(`row-${CI_ID_SCOPE}-a/b`)
    expect(control('Open CI health o/r')).toHaveAttribute('tabindex', '-1')
    first.focus()
    fireEvent.keyDown(first, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(control('Open CI health o/r'))
    expect(control('Open CI health o/r')).toHaveAttribute('tabindex', '0')
  })

  it('CI health scope: one row per repo with the 7-day meta line, led by the lane word', () => {
    // No CI/CD Doctor row in the fixture → thresholds unknown → the flake is
    // counted, not judged, and the verdict rests on regressions (none): green.
    const { onOpenRepo } = mount({ scope: 'ci' })
    const list = screen.getByRole('list', { name: 'CI health' })
    expect(within(list).getAllByRole('listitem')).toHaveLength(2)
    expect(screen.getByText('green · 1 flaky · slowest e2e 6m 40s')).toBeInTheDocument()
    expect(screen.getByText('No check history in 7d')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Open CI health o/r' }))
    expect(onOpenRepo).toHaveBeenCalledWith('o/r')
    expect(screen.getByTestId(`row-${ciRepoKey('a/b')}`)).toBeInTheDocument()
  })
  // 2026-10-01 Delivery critique, P2: a neutral rail and "1 flaky" with no
  // judgement. With the doctor's thresholds the row reads a verdict in the
  // pipeline's lane vocabulary, and the rail takes its tone.
  it('CI health scope: with the CI/CD Doctor\'s thresholds a 20% flake is red on the rail and in words', () => {
    const doctor = {
      ...AUTOMATIONS[0], id: 'cicd-doctor', name: 'CI/CD Doctor', config: {},
      configSchema: [
        { key: 'max_flake_pct', label: '', type: 'int' as const, default: 2, help: '', choices: null },
        { key: 'demote_flake_pct', label: '', type: 'int' as const, default: 5, help: '', choices: null },
      ],
    }
    mount({ scope: 'ci', automations: [...AUTOMATIONS, doctor] })
    expect(screen.getByText('red · 1 flaky ≥5% · slowest e2e 6m 40s')).toBeInTheDocument()
    const row = screen.getByTestId(`row-${ciRepoKey('o/r')}`)
    expect(row.querySelector('.bg-danger')).not.toBeNull()
    // The repo with no history keeps the muted idle rail.
    expect(screen.getByTestId(`row-${ciRepoKey('a/b')}`).querySelector('[class*="bg-neutral/20"]')).not.toBeNull()
  })
  it('the scope segment links all three scopes and counts them', () => {
    mount()
    expect(screen.getByRole('link', { name: 'Automations (3)' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('link', { name: 'CI health (2)' })).toHaveAttribute('href', '/delivery?scope=ci')
    expect(screen.getByRole('link', { name: 'Capacity' })).toHaveAttribute('href', '/delivery?scope=capacity')
  })
  it('Capacity scope: the five stack rows, and a binding constraint as attention on the scope link', () => {
    mount({ scope: 'capacity', capacity: CAPACITY })
    const list = screen.getByRole('list', { name: 'Capacity' })
    expect(within(list).getAllByRole('listitem')).toHaveLength(5)
    expect(screen.getByRole('link', { name: 'Capacity (needs attention)' })).toHaveAttribute('aria-current', 'page')
  })
  // 2026-10-01 Delivery re-critique, P0: below desktop the pane with the
  // controls was unreachable. The column carries the way into it (hidden from
  // the desktop breakpoint, where the pane is already beside the list).
  it('Capacity scope: offers the way into the controls pane below desktop', () => {
    mount({ scope: 'capacity', capacity: CAPACITY })
    const link = screen.getByRole('link', { name: 'Open capacity controls' })
    expect(link).toHaveAttribute('href', '/delivery/capacity/governor')
    expect(link.className).toMatch(/\blg:hidden\b/)
    expect(link.className).toMatch(/\bmin-h-target\b/)
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

describe('DeliveryListColumn — failed reads (#1405)', () => {
  it('a failed first load says it failed, not that nothing is registered, and offers Retry', () => {
    const onRetryRepos = vi.fn()
    mount({ scope: 'ci', repos: [], reposError: true, onRetryRepos })
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load CI health.')
    expect(screen.queryByText(/No repositories with pipeline history/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(onRetryRepos).toHaveBeenCalledTimes(1)
  })
  it('a failed refresh keeps the last-good rows and labels them stale', () => {
    mount({ scope: 'ci', reposError: true, onRetryRepos: vi.fn() })
    expect(screen.getByRole('alert')).toHaveTextContent(/may be out of date/)
    expect(screen.getAllByRole('button', { name: /^Open/ }).length).toBeGreaterThan(0)
  })
  it('automations: failed first load hides the empty copy and retries', () => {
    const onRetryAutomations = vi.fn()
    mount({ automations: [], automationsError: true, onRetryAutomations })
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load automations.')
    expect(screen.queryByText(/No automations/i)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(onRetryAutomations).toHaveBeenCalledTimes(1)
  })
})
