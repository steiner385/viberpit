/**
 * Settings › Capacity › Limits (spec 2026-09-20 §5.1). Hooks are mocked, as in
 * GeneralSection.test.tsx: this suite is about the rows, the payload and the
 * readout card, not the transport.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { axe, toHaveNoViolations } from 'jest-axe'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import * as hooks from '@/api/hooks'
import type { GlobalCapDefaults } from '@/api/hooks'
import { appSettings } from '@/test/factories'
import { queryResult, idleQueryResult, mutationResult } from '@/test/reactQuery'
import { LimitsSection, effectiveLine, physicalLine } from './LimitsSection'

expect.extend(toHaveNoViolations)

vi.mock('@/api/hooks', () => ({
  useSettings: vi.fn(),
  useUpdateSettings: vi.fn(),
  useCapacity: vi.fn(),
}))

const TWO_HOSTS: GlobalCapDefaults = {
  physical: 10, attention: null, effective: 10, binding: 'physical',
  hosts: [{ name: 'dobby', cap: 4 }, { name: 'rocky', cap: 6 }],
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createMemoryRouter([{ path: '/', element: <LimitsSection /> }])
  return render(<QueryClientProvider client={qc}><RouterProvider router={router} /></QueryClientProvider>)
}

describe('physicalLine / effectiveLine', () => {
  it('names the hosts behind the physical ceiling', () => {
    expect(physicalLine(TWO_HOSTS)).toBe('10 — dobby 4 · rocky 6')
  })
  it('names the uncapped host when unbounded', () => {
    expect(physicalLine({ ...TWO_HOSTS, physical: null, hosts: [{ name: 'dobby', cap: 4 }, { name: 'rocky', cap: null }] }))
      .toBe('unbounded — rocky has no cap')
    expect(physicalLine({ ...TWO_HOSTS, physical: null, hosts: [] })).toBe('unbounded — no hosts registered')
  })
  it('says which ceiling binds', () => {
    expect(effectiveLine(TWO_HOSTS)).toBe('10 — the physical ceiling binds')
    expect(effectiveLine({ ...TWO_HOSTS, attention: 6, effective: 6, binding: 'attention' }))
      .toBe('6 — the attention ceiling binds')
    expect(effectiveLine({ ...TWO_HOSTS, attention: 10, effective: 10, binding: 'attention' }))
      .toBe('10 — the attention ceiling binds (equal to the physical ceiling)')
    expect(effectiveLine({ physical: null, attention: null, effective: 5, binding: 'default', hosts: [] }))
      .toBe('5 — default; set a host cap on Hosts or an attention ceiling')
  })
})

describe('LimitsSection', () => {
  const mutateAsync = vi.fn().mockResolvedValue({})
  beforeEach(() => {
    mutateAsync.mockClear()
    vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings({
      maxActiveSessions: null,
      managed: [
        { key: 'CCSWITCH_ENABLE_COPILOT', value: false, description: 'Enable GitHub Copilot accounts' },
        { key: 'CCSWITCH_TEST_SLOT_CAP', value: 1, description: 'Full test-suite runs one host may carry at once' },
        { key: 'VIBERPIT_CAPACITY_GOVERNOR_ENABLED', value: true, description: 'Capacity governor may apply its plan (act mode)' },
      ],
      effectiveDefaults: {
        repoRoot: '/r', archiveHome: null, modelApprovalAction: 'require_approval', modelApprovalDenylist: 'fable',
        globalCap: TWO_HOSTS,
      },
    })))
    vi.mocked(hooks.useUpdateSettings).mockReturnValue(mutationResult({ mutateAsync }))
    vi.mocked(hooks.useCapacity).mockReturnValue(idleQueryResult())
  })

  it('shows the three global-cap rows and the physical value as the attention placeholder', async () => {
    const { container } = mount()
    expect(screen.getByRole('heading', { level: 2, name: 'Limits' })).toBeInTheDocument()
    expect(screen.getByText('10 — dobby 4 · rocky 6')).toBeInTheDocument()
    expect(screen.getByLabelText('Attention ceiling')).toHaveAttribute('placeholder', 'None · physical ceiling 10 applies')
    expect(screen.getByText('10 — the physical ceiling binds')).toBeInTheDocument()
    expect(await axe(container)).toHaveNoViolations()
  })

  it('saves the attention ceiling as maxActiveSessions and blank as null', async () => {
    mount()
    await userEvent.type(screen.getByLabelText('Attention ceiling'), '6')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(mutateAsync).toHaveBeenCalledWith({ maxActiveSessions: 6 })
    await userEvent.clear(screen.getByLabelText('Attention ceiling'))
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(mutateAsync).toHaveBeenLastCalledWith({ maxActiveSessions: null })
  })

  it('links "unbounded" to Hosts', () => {
    vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings({
      effectiveDefaults: {
        repoRoot: '/r', archiveHome: null, modelApprovalAction: 'require_approval', modelApprovalDenylist: 'fable',
        globalCap: { physical: null, attention: null, effective: 5, binding: 'default', hosts: [{ name: 'rocky', cap: null }] },
      },
    })))
    mount()
    expect(screen.getByRole('link', { name: 'Set a cap on Hosts' })).toHaveAttribute('href', '/settings/fleet')
  })

  it('shows only the capacity env flags, and the live readout card when the readout loads', () => {
    vi.mocked(hooks.useCapacity).mockReturnValue(queryResult({
      computedAt: '2026-09-20T10:00:00Z', pushLevel: 'steady', governorMode: 'act', killSwitch: true,
      global: { cap: 10, active: 5, free: 5, physical: 10, attention: null, binding: 'physical' },
      hosts: [], accounts: { available: 4, total: 7, reservePct: 20, nextResetAt: null, rows: [] },
      runner: { ceiling: 6, effective: 6, inFlight: 4, derived: null },
      products: [], binding: { layer: 'product', code: 'product_capacity_full', text: 'ghostdriver: The product capacity limit is reached (1/1).' },
      defects: [], plan: null, planLive: false,
    } as never))
    mount()
    const names = screen.getAllByRole('rowheader').map((th) => th.textContent)
    expect(names).toEqual(['CCSWITCH_TEST_SLOT_CAP', 'VIBERPIT_CAPACITY_GOVERNOR_ENABLED'])
    expect(screen.getByText('Global cap')).toBeInTheDocument()
    expect(screen.getByText('5 / 10 · 5 free')).toBeInTheDocument()
    expect(screen.getByText(/ghostdriver: The product capacity limit/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open Delivery → Capacity' })).toHaveAttribute('href', '/delivery?scope=capacity')
  })
})
