import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { CAPACITY } from './__fixtures__/capacity'
import { CapacityDetail } from './CapacityDetail'

const patchCalls: { id: string; body: unknown }[] = []
const updateProjectCalls: { slug: string; input: unknown }[] = []
const updateProductPolicyCalls: { slug: string; input: unknown }[] = []

const GOVERNOR = {
  id: 'capacity-governor', name: 'Capacity Governor', description: '', enabled: true,
  mode: 'monitor', modes: ['off', 'monitor', 'act'],
  status: { text: 'Monitoring', level: 'idle' as const, counts: null },
  enginePolicy: 'claude_only' as const, operatorTriggered: false,
  configSchema: [], config: {}, schedule: null, lastActiveAt: null, activeSessions: 0,
  oversight: {
    responsibility: { id: 'execution', label: 'Execute and finish work' },
    scope: 'Fleet capacity', modeDescriptions: { monitor: 'Computes and publishes a plan without applying it.' },
    relationships: [],
  },
}

vi.mock('@/api/hooks', () => ({
  useCapacity: () => ({ data: CAPACITY, isLoading: false, isError: false }),
  useAutomations: () => ({ data: [GOVERNOR], isLoading: false, isError: false }),
  usePatchAutomation: () => ({
    mutate: (args: { id: string; body: unknown }) => patchCalls.push(args),
    isPending: false,
  }),
  useUpdateProject: (slug: string) => ({
    mutate: (input: unknown) => updateProjectCalls.push({ slug, input }),
  }),
  useUpdateProductPolicy: (slug: string) => ({
    mutate: (input: unknown) => updateProductPolicyCalls.push({ slug, input }),
  }),
  useProject: () => ({ data: undefined }),
}))

function mount() {
  render(<MemoryRouter><CapacityDetail /></MemoryRouter>)
}

afterEach(() => {
  patchCalls.length = 0
  updateProjectCalls.length = 0
  updateProductPolicyCalls.length = 0
})

describe('CapacityDetail', () => {
  it('renders the binding text', () => {
    mount()
    expect(screen.getByText(CAPACITY.binding!.text)).toBeInTheDocument()
  })

  it('lists products in rank order with pinned/auto badges and a defect link', () => {
    mount()
    const rows = screen.getAllByRole('row').slice(1) // drop the header row
    expect(within(rows[0]!).getByText('KinDash')).toBeInTheDocument()
    expect(within(rows[0]!).getByText('pinned')).toBeInTheDocument()
    expect(within(rows[1]!).getByText('ViberPit')).toBeInTheDocument()
    expect(within(rows[1]!).getByText('auto')).toBeInTheDocument()
    const defectLink = within(rows[1]!).getByRole('link', { name: 'defect' })
    expect(defectLink).toHaveAttribute('href', '/sessions/sess-1')
  })

  it('clicking Move up on the second row swaps its rank with the first', () => {
    mount()
    const moveUpButtons = screen.getAllByRole('button', { name: 'Move up' })
    fireEvent.click(moveUpButtons[1]!) // the second row (ViberPit)
    expect(updateProjectCalls).toContainEqual({ slug: 'viberpit', input: { priorityRank: 1 } })
    expect(updateProjectCalls).toContainEqual({ slug: 'kindash', input: { priorityRank: 2 } })
  })

  it('the first row cannot move up', () => {
    mount()
    const moveUpButtons = screen.getAllByRole('button', { name: 'Move up' })
    expect(moveUpButtons[0]).toBeDisabled()
  })

  it('the push-level radio patches the governor config', () => {
    mount()
    fireEvent.click(screen.getByRole('radio', { name: 'Max' }))
    expect(patchCalls).toContainEqual({ id: 'capacity-governor', body: { config: { push_level: 'max' } } })
  })

  it('the governor mode select patches the mode', () => {
    mount()
    fireEvent.change(screen.getByRole('combobox', { name: 'Governor mode' }), { target: { value: 'act' } })
    expect(patchCalls).toContainEqual({ id: 'capacity-governor', body: { mode: 'act' } })
  })

  it('the plan card lists the plan reasons', () => {
    mount()
    for (const reason of CAPACITY.plan!.reasons) {
      expect(screen.getByText(reason)).toBeInTheDocument()
    }
  })

  it('toggling cap mode patches the product policy', () => {
    mount()
    fireEvent.click(screen.getByRole('switch', { name: 'ViberPit pinned' }))
    expect(updateProductPolicyCalls).toContainEqual({
      slug: 'viberpit', input: { policy: { capacity_mode: 'pinned' }, expectedUpdatedAt: null },
    })
  })
})
