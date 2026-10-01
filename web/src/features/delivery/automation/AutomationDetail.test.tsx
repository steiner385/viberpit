import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { AUTOMATIONS } from '../__fixtures__/automations'
import { AutomationDetail } from './AutomationDetail'
import { AutomationControls } from './AutomationControls'

const patch = vi.fn(); const trigger = vi.fn().mockResolvedValue({ status: 'done', level: 'ok', message: 'Fixed' })
// A mutable variable (not a fixed object literal) so individual tests can
// simulate the actionable list not having answered yet (F3: the Needs you
// badge follows the FETCHED list, falling back to the payload's own
// `actionableCount` only while that fetch is still in flight).
let actionableData: unknown[] | undefined = [{ id: 'o/r#1', title: 'CI red on #1', actions: [{ kind: 'fix', label: 'Fix', style: 'safe' }] }]
vi.mock('@/api/hooks', () => ({
  usePatchAutomation: () => ({ mutate: patch, isPending: false }),
  useAutomationEvents: () => ({ data: [{ ts: '2026-09-11T10:00:00Z', level: 'info', kind: null, summary: 'scanned 3 PRs', target: null, sessionId: 's1' }] }),
  useAutomationSessions: () => ({ data: [{ id: 's1', name: 'fix #1', status: 'in_progress', originRef: null }, { id: 's2', name: 'old', status: 'ended', originRef: null }] }),
  useAutomationActionable: () => ({ data: actionableData }),
  useAutomationTrigger: () => ({ mutateAsync: trigger, isPending: false }),
  useGithubRepos: () => ({ data: [{ nameWithOwner: 'o/r' }], isLoading: false }),
  useConsolidation: () => ({ data: undefined }), useConsolidate: () => ({ mutate: vi.fn(), isPending: false }),
}))
vi.mock('./ConsolidatePanel', () => ({ ConsolidatePanel: () => <div data-testid="consolidate" /> }))
vi.mock('./BacklogGroupsPanel', () => ({ BacklogGroupsPanel: () => <div data-testid="groups" /> }))
vi.mock('./ManualDispatchDialog', () => ({ ManualDispatchDialog: () => null }))

const prDoctor = AUTOMATIONS[0]
const mount = (a = prDoctor, path = '/delivery/automations/pr-doctor') => {
  const onOpenSession = vi.fn()
  const r = render(<MemoryRouter initialEntries={[path]}><AutomationDetail automation={a} onOpenSession={onOpenSession} /></MemoryRouter>)
  return { ...r, onOpenSession }
}

describe('AutomationDetail', () => {
  beforeEach(() => {
    patch.mockReset(); trigger.mockClear()
    actionableData = [{ id: 'o/r#1', title: 'CI red on #1', actions: [{ kind: 'fix', label: 'Fix', style: 'safe' }] }]
  })
  it('Overview: description, oversight and a three-row actionable preview — no queue surface', async () => {
    mount()
    expect(screen.getByRole('tab', { name: 'Overview', selected: true })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Actionable items' })).toBeInTheDocument()
    expect(screen.getByText('CI red on #1')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Fix' }))
    await waitFor(() => expect(trigger).toHaveBeenCalledWith({ item_id: 'o/r#1', kind: 'fix' }))
    // The consolidation panel is the Queue tab's, not the Overview's.
    expect(screen.queryByTestId('consolidate')).not.toBeInTheDocument()
  })
  it('Needs you: every automation has the tab; the badge follows the FETCHED list, with attention above zero (F3)', () => {
    // The mock answers the same one-item list regardless of automation id
    // (it shares `useAutomationActionable`'s query key with the panel), so
    // every automation here shows the fetched length once it has loaded —
    // not the list payload's own (possibly stale) `actionableCount`.
    let r = mount()
    expect(screen.getByRole('tab', { name: 'Needs you (1, needs attention)' })).toBeInTheDocument()
    r.unmount()
    r = mount(AUTOMATIONS[1], '/delivery/automations/backlog-runner')
    expect(screen.getByRole('tab', { name: 'Needs you (1, needs attention)' })).toBeInTheDocument()
    r.unmount()
    // Review focus 2: operator-triggered still has the question.
    mount(AUTOMATIONS[2], '/delivery/automations/pm-classify')
    expect(screen.getByRole('tab', { name: 'Needs you (1, needs attention)' })).toBeInTheDocument()
  })
  it('Needs you: the fetched list length wins even when actionableCount is null (F3)', () => {
    // Review focus 1, updated: `actionableCount: null` used to force a bare
    // tab; now the list has loaded (1 item) and that is what counts.
    mount({ ...prDoctor, actionableCount: null }, '/delivery/automations/pr-doctor')
    expect(screen.getByRole('tab', { name: 'Needs you (1, needs attention)' })).toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: /NaN/ })).toBeNull()
  })
  it('Needs you: falls back to the payload actionableCount while the fetched list has not answered (F3)', () => {
    actionableData = undefined
    mount(AUTOMATIONS[1], '/delivery/automations/backlog-runner') // actionableCount: 2
    expect(screen.getByRole('tab', { name: 'Needs you (2, needs attention)' })).toBeInTheDocument()
  })
  it('Needs you: an unknown actionableCount is a bare tab while the list has not answered either, not NaN (F3)', () => {
    actionableData = undefined
    mount({ ...prDoctor, actionableCount: null }, '/delivery/automations/pr-doctor')
    expect(screen.getByRole('tab', { name: 'Needs you' })).toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: /NaN/ })).toBeNull()
  })
  it('Needs you: ?tab=needs-you selects it and shows the full list; the order is Overview · Needs you · Queue …', () => {
    mount(prDoctor, '/delivery/automations/pr-doctor?tab=needs-you')
    // The badge follows the fetched list (F3), and the mock list has one item.
    expect(screen.getByRole('tab', { name: 'Needs you (1, needs attention)', selected: true })).toBeInTheDocument()
    expect(screen.getByText('CI red on #1')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Actionable items' })).toBeNull()
    const names = screen.getAllByRole('tab').map((t) => t.getAttribute('aria-label'))
    expect(names.slice(0, 3)).toEqual(['Overview', 'Needs you (1, needs attention)', 'Queue'])
  })
  it('Queue: the PR doctor consolidation panel', () => {
    mount(prDoctor, '/delivery/automations/pr-doctor?tab=queue')
    expect(screen.getByRole('tab', { name: 'Queue', selected: true })).toBeInTheDocument()
    expect(screen.getByTestId('consolidate')).toBeInTheDocument()
  })
  it('Queue: the runner’s products table, groups panel and custom dispatch', () => {
    const runner = {
      ...AUTOMATIONS[1],
      status: {
        ...AUTOMATIONS[1].status,
        products: [{ slug: 'viberpit', dispatch: 'auto', inFlight: 1, cap: 1, eligible: 12, bounced: 2 }],
      },
    }
    mount(runner, '/delivery/automations/backlog-runner?tab=queue')
    expect(screen.getByRole('link', { name: 'viberpit' })).toHaveAttribute('href', '/roadmap/viberpit')
    expect(screen.getByTestId('groups')).toBeInTheDocument()
    // The dispatch action sits on the heading of the list it adds to.
    expect(screen.getByRole('heading', { name: 'Planned groups' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Custom dispatch…' })).toBeInTheDocument()
  })
  it('Overview says what the configured mode means; a panel never restates its tab as a heading', () => {
    mount()
    expect(screen.getByText(/Repairs eligible PRs within configured permissions/)).toBeInTheDocument()
    mount(prDoctor, '/delivery/automations/pr-doctor?tab=config')
    expect(screen.queryByRole('heading', { name: /Configuration/ })).toBeNull()
    mount(prDoctor, '/delivery/automations/pr-doctor?tab=schedule')
    expect(screen.queryByRole('heading', { name: /Schedule/ })).toBeNull()
  })
  it('an automation with no dispatchable work has no Queue tab, and ?tab=status lands on Overview', () => {
    mount(AUTOMATIONS[2], '/delivery/automations/pm-classify?tab=status')
    expect(screen.queryByRole('tab', { name: 'Queue' })).not.toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Overview', selected: true })).toBeInTheDocument()
  })
  it('tabs are URL state', () => {
    mount(prDoctor, '/delivery/automations/pr-doctor?tab=events')
    expect(screen.getByRole('tab', { name: 'Events', selected: true })).toBeInTheDocument()
    expect(screen.getByText('scanned 3 PRs')).toBeInTheDocument()
    // The event's time is a real <time> on the ISO stamp, rendered in the
    // reader's clock rather than a UTC substring.
    expect(document.querySelector('time')).toHaveAttribute('dateTime', '2026-09-11T10:00:00Z')
    expect(screen.queryByRole('heading', { name: 'Activity' })).toBeNull()
  })
  it('Sessions tab links live sessions by work class and ended ones to the archive', () => {
    mount(prDoctor, '/delivery/automations/pr-doctor?tab=sessions')
    // The tab wears the count (standard §7); the panel adds how many are active.
    expect(screen.getByRole('tab', { name: 'Sessions (2)', selected: true })).toBeInTheDocument()
    expect(screen.getByText('1 active')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /fix #1/ })).toHaveAttribute('href', '/sessions/background/s1')
    expect(screen.getByRole('link', { name: /old/ })).toHaveAttribute('href', '/sessions/archived/s2')
    // The status is the app's label for it, not the wire key (spec 2026-09-28 §4).
    expect(screen.getByText('· Working…')).toBeInTheDocument()
    expect(screen.getByText('· Ended')).toBeInTheDocument()
    expect(screen.queryByText('· in_progress')).toBeNull()
  })
  it('Config tab renders the schema and patches on change', () => {
    mount(prDoctor, '/delivery/automations/pr-doctor?tab=config')
    fireEvent.click(screen.getByRole('checkbox', { name: 'Auto-fix' }))
    expect(patch).toHaveBeenCalledWith({ id: 'pr-doctor', body: { config: expect.objectContaining({ auto_fix: false }) } })
  })
  it('Config tab: a Capacity-owned field is read-only and links there (spec 2026-09-20 D7)', () => {
    mount(AUTOMATIONS[1], '/delivery/automations/backlog-runner?tab=config')
    expect(screen.queryByRole('spinbutton', { name: 'Runner ceiling' })).toBeNull()
    expect(screen.getByText('Runner ceiling')).toBeInTheDocument()
    expect(screen.getByText('5')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Set on Delivery → Capacity' })).toHaveAttribute('href', '/delivery?scope=capacity')
    expect(screen.getByRole('spinbutton', { name: 'Max issues per PR' })).toBeInTheDocument()
  })
  it('Config tab: a deprecated field is hidden entirely (spec 2026-09-20 D6)', () => {
    const runner = {
      ...AUTOMATIONS[1],
      configSchema: [
        ...AUTOMATIONS[1].configSchema,
        { key: 'old_knob', label: 'Old knob', type: 'int' as const, default: 1, help: '', choices: null, deprecated: true },
      ],
      config: { ...AUTOMATIONS[1].config, old_knob: 3 },
    }
    mount(runner, '/delivery/automations/backlog-runner?tab=config')
    expect(screen.queryByText('Old knob')).toBeNull()
    expect(screen.queryByRole('spinbutton', { name: 'Old knob' })).toBeNull()
    // The surrounding fields still render.
    expect(screen.getByRole('spinbutton', { name: 'Max issues per PR' })).toBeInTheDocument()
  })
  it('an operator-triggered automation has no Schedule tab and no switch', () => {
    mount(AUTOMATIONS[2], '/delivery/automations/pm-classify')
    expect(screen.queryByRole('tab', { name: 'Schedule' })).not.toBeInTheDocument()
    render(<MemoryRouter><AutomationControls automation={AUTOMATIONS[2]} /></MemoryRouter>)
    expect(screen.queryByRole('switch')).not.toBeInTheDocument()
  })
  it('controls: mode select and enable switch patch the automation', () => {
    render(<MemoryRouter><AutomationControls automation={prDoctor} /></MemoryRouter>)
    fireEvent.change(screen.getByRole('combobox', { name: 'Configured mode' }), { target: { value: 'propose' } })
    expect(patch).toHaveBeenCalledWith({ id: 'pr-doctor', body: { mode: 'propose' } })
    fireEvent.click(screen.getByRole('switch', { name: 'PR doctor enabled' }))
    expect(patch).toHaveBeenCalledWith({ id: 'pr-doctor', body: { enabled: false } })
  })
  it('Queue: a per-product table when status.products is non-empty, linking to the roadmap', () => {
    const runner = {
      ...AUTOMATIONS[1],
      status: {
        ...AUTOMATIONS[1].status,
        products: [{ slug: 'viberpit', dispatch: 'auto', inFlight: 1, cap: 1, eligible: 12, bounced: 2 }],
      },
    }
    mount(runner, '/delivery/automations/backlog-runner?tab=queue')
    expect(screen.getByRole('link', { name: 'viberpit' })).toHaveAttribute('href', '/roadmap/viberpit')
    // Scoped to the table: the Sessions tab's count badge also reads "2".
    const table = within(screen.getByRole('table'))
    expect(table.getByText('auto')).toBeInTheDocument()
    expect(table.getByText('1/1')).toBeInTheDocument()
    expect(table.getByText('12')).toBeInTheDocument()
    expect(table.getByText('2')).toBeInTheDocument()
  })
  it('shows active dispatch-off work and explicitly accounts for unassigned work', () => {
    const runner = { ...AUTOMATIONS[1], status: { text: '3 work items in flight', level: 'active' as const, counts: { unassigned_in_flight: 2 },
      products: [{ slug: 'paused-product', dispatch: 'off', inFlight: 1, cap: 2, eligible: 0, bounced: 0 }] } }
    mount(runner)
    expect(screen.getByText('2 in-flight work items are not assigned to a product.')).toBeInTheDocument()
    mount(runner, '/delivery/automations/backlog-runner?tab=queue')
    expect(screen.getByText('off')).toBeInTheDocument()
    expect(screen.getByText('1/2')).toBeInTheDocument()
  })
  it('does not turn unavailable product or unassigned workload into zero', () => {
    mount({ ...AUTOMATIONS[1], status: { text: 'Unavailable', level: 'unknown', counts: null, products: null } })
    expect(screen.getByText('Product workload unavailable.')).toBeInTheDocument()
    expect(screen.getByText('Unassigned workload unknown.')).toBeInTheDocument()
  })
  it('Queue: no per-product table when status.products is null', () => {
    mount(prDoctor, '/delivery/automations/pr-doctor?tab=queue')
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })
  it('Queue: no per-product section at all when status.products is empty', () => {
    // The runner answers `[]` while no product has dispatch on. `CheckTable` with
    // no rows renders its heading and empty state but NO <table>, so a bare
    // "no table" assertion would miss a dropped `.length > 0` guard — the card
    // would grow an empty "Products" section reading like a broken read.
    const runner = { ...AUTOMATIONS[1], status: { ...AUTOMATIONS[1].status, products: [] } }
    mount(runner, '/delivery/automations/backlog-runner?tab=queue')
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Products' })).not.toBeInTheDocument()
    expect(screen.queryByText('No products yet.')).not.toBeInTheDocument()
  })
  it('renders status.counts as stat tiles above the strip, and no band when counts is null', () => {
    const runner = { ...AUTOMATIONS[1], status: { ...AUTOMATIONS[1].status, counts: { in_flight: 2, eligible: 14, blocked: 63 } } }
    const { unmount } = mount(runner, '/delivery/automations/backlog-runner')
    const band = screen.getByText('In flight').closest('dl')!
    expect(band.compareDocumentPosition(screen.getByRole('tablist'))).toBe(Node.DOCUMENT_POSITION_FOLLOWING)
    expect(screen.getByText('14')).toBeInTheDocument()
    unmount()
    mount(prDoctor) // counts: null
    expect(document.querySelector('dl')).toBeNull()
  })
  // ── the phone fold (2026-10-01 Delivery critique, P1) ──
  it('the band leads with a clickable Needs you tile that wears the tab badge number and opens the tab; four tiles at most', () => {
    const runner = { ...AUTOMATIONS[1], status: { ...AUTOMATIONS[1].status,
      counts: { in_flight: 2, needs_human: 4, eligible: 14, held: 1, blocked: 63, untriaged: 7 } } }
    mount(runner, '/delivery/automations/backlog-runner')
    const labels = [...document.querySelectorAll('dl dt')].map((dt) => dt.textContent)
    expect(labels).toEqual(['Needs you', 'In flight', 'Needs human', 'Eligible'])
    // Same number as the tab (the fetched list has one item), and it opens it.
    expect(screen.getByRole('tab', { name: 'Needs you (1, needs attention)' })).toHaveAttribute('aria-selected', 'false')
    fireEvent.click(screen.getByRole('button', { name: 'Needs you: 1' }))
    expect(screen.getByRole('tab', { name: 'Needs you (1, needs attention)' })).toHaveAttribute('aria-selected', 'true')
  })
  it('omits the Needs you tile while the count is unknown, without a fourth server tile sliding into its place', () => {
    actionableData = undefined
    const runner = { ...AUTOMATIONS[1], actionableCount: null,
      status: { ...AUTOMATIONS[1].status, counts: { in_flight: 2, eligible: 14, blocked: 63, held: 1 } } }
    mount(runner, '/delivery/automations/backlog-runner')
    expect([...document.querySelectorAll('dl dt')].map((dt) => dt.textContent)).toEqual(['In flight', 'Eligible', 'Blocked'])
  })
  it('the tab strip sticks to the top of the page scroller, on the canvas', () => {
    mount()
    const strip = screen.getByRole('tablist')
    expect(strip.className).toMatch(/\bsticky\b/)
    expect(strip.className).toMatch(/\btop-0\b/)
    expect(strip.className).toMatch(/\bbg-canvas\b/)
  })
})
