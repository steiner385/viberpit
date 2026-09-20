import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ApiRequestError } from '@/api/client'
import { QK } from '@/api/hooks'
import { mutationResult, queryResult } from '@/test/reactQuery'
import { CAPACITY } from './__fixtures__/capacity'
import { CapacityDetail } from './CapacityDetail'

const updateProductPolicy = vi.fn().mockResolvedValue({})
vi.mock('@/api/pm', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/pm')>()
  return { ...actual, updateProductPolicy: (...args: unknown[]) => updateProductPolicy(...args) }
})

// `SaveBar`'s `useBlocker` (inside `useSettingsForm`) requires a DATA router —
// a plain `<MemoryRouter>` throws "useBlocker used outside a data router".
vi.mock('@/api/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/hooks')>()
  return {
    ...actual,
    useCapacity: vi.fn(),
    useAutomations: vi.fn(),
    usePatchAutomation: vi.fn(),
    useUpdateProductPolicy: vi.fn(),
    useProject: vi.fn(),
    useReorderProject: vi.fn(),
  }
})

import * as hooks from '@/api/hooks'

const GOVERNOR = {
  id: 'capacity-governor', name: 'Capacity Governor', description: '', enabled: true,
  mode: 'monitor', modes: ['off', 'monitor', 'act'],
  status: { text: 'Monitoring', level: 'idle' as const, counts: null },
  enginePolicy: 'claude_only' as const, operatorTriggered: false,
  configSchema: [], config: { push_level: 'steady', sessions_per_account: 2 }, schedule: null, lastActiveAt: null, activeSessions: 0,
  oversight: {
    responsibility: { id: 'execution', label: 'Execute and finish work' },
    scope: 'Fleet capacity', modeDescriptions: { monitor: 'Computes and publishes a plan without applying it.' },
    relationships: [],
  },
}

const RUNNER = {
  id: 'backlog-runner', name: 'Backlog runner', description: '', enabled: true,
  mode: 'on', modes: ['off', 'on'],
  status: { text: 'Idle', level: 'idle' as const, counts: null },
  enginePolicy: 'claude_only' as const, operatorTriggered: false,
  configSchema: [], config: { max_concurrent: 5, max_spawns_per_day: 10 }, schedule: null, lastActiveAt: null, activeSessions: 0,
}

const patchMutate = vi.fn()
const patchMutateAsync = vi.fn().mockResolvedValue({})
const reorderMutateAsync = vi.fn().mockResolvedValue({})
const patchPolicyMutate = vi.fn()

function setup({
  patchState = mutationResult({ mutate: patchMutate, mutateAsync: patchMutateAsync }),
  patchPolicyState = mutationResult({ mutate: patchPolicyMutate }),
  projectData,
  readout = CAPACITY,
  automations = [GOVERNOR, RUNNER],
}: {
  patchState?: ReturnType<typeof mutationResult>
  patchPolicyState?: ReturnType<typeof mutationResult>
  projectData?: { automationPolicyUpdatedAt: string | null }
  readout?: typeof CAPACITY
  automations?: (typeof GOVERNOR | typeof RUNNER)[]
} = {}) {
  vi.mocked(hooks.useCapacity).mockReturnValue(queryResult(readout) as never)
  vi.mocked(hooks.useAutomations).mockReturnValue(queryResult(automations) as never)
  // `usePatchAutomation()` is called THREE times per render (push-level
  // radio, governor-mode select, and the runner/governor half of the one
  // capacity form — three SEPARATE hook instances so the two status-bearing
  // controls each show their own Saving/Saved/Failed rather than borrowing
  // whichever the operator touched last). A `vi.fn()` mock has no notion of
  // "hook call position" the way React's real hook slots do, and
  // `useSettingsForm`'s render-time state adjustment (seeding the form draft)
  // makes React restart this component's render at least once before
  // settling — so a `mockReturnValueOnce` chain keyed to call order silently
  // drifts onto the wrong control once that restart consumes the first few
  // slots. One shared state for all three is the fixture this file can make
  // honestly; the independence itself is what the "two independent
  // usePatchAutomation() instances" test below (checking a multiple-of-2
  // raw count from the two status-bearing controls) stands for.
  vi.mocked(hooks.usePatchAutomation).mockReturnValue(patchState as never)
  vi.mocked(hooks.useUpdateProductPolicy).mockReturnValue(patchPolicyState as never)
  vi.mocked(hooks.useProject).mockReturnValue(queryResult(projectData ?? { automationPolicyUpdatedAt: null }) as never)
  vi.mocked(hooks.useReorderProject).mockReturnValue(
    mutationResult({ mutateAsync: reorderMutateAsync }) as never,
  )

  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createMemoryRouter([{ path: '/', element: <CapacityDetail /> }])
  return {
    qc,
    ...render(
      <QueryClientProvider client={qc}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    ),
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  updateProductPolicy.mockResolvedValue({})
  reorderMutateAsync.mockResolvedValue({})
  patchMutateAsync.mockResolvedValue({})
})

describe('CapacityDetail', () => {
  it('renders the binding text', () => {
    setup()
    expect(screen.getByText(CAPACITY.binding!.text)).toBeInTheDocument()
  })

  it('the binding row is not a live region (M7) — content changes every poll', () => {
    setup()
    const row = screen.getByTestId('capacity-stack-products')
    // `role="none"` on the Banner means no ARIA live-region role leaks onto it.
    expect(within(row).queryByRole('status')).toBeNull()
    expect(within(row).queryByRole('alert')).toBeNull()
  })

  it('lists products in rank order with pinned/auto badges and a defect link', () => {
    setup()
    const rows = screen.getAllByRole('row').slice(1) // drop the header row
    expect(within(rows[0]!).getByText('KinDash')).toBeInTheDocument()
    expect(within(rows[0]!).getByText('pinned')).toBeInTheDocument()
    expect(within(rows[1]!).getByText('ViberPit')).toBeInTheDocument()
    expect(within(rows[1]!).getByText('auto')).toBeInTheDocument()
    const defectLink = within(rows[1]!).getByRole('link', { name: 'defect' })
    expect(defectLink).toHaveAttribute('href', '/sessions/sess-1')
  })

  it('M9: the table names itself and its header/row cells', () => {
    setup()
    const table = screen.getByRole('table', { name: 'Products' })
    expect(table).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'Rank' })).toBeInTheDocument()
    expect(screen.getByRole('rowheader', { name: 'KinDash' })).toBeInTheDocument()
  })

  it('M8: rank buttons name the product they move', () => {
    setup()
    expect(screen.getByRole('button', { name: 'Move KinDash up' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Move KinDash down' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Move ViberPit up' })).toBeEnabled()
  })

  it('I5: clicking Move up issues the mover patch, then the neighbour patch, in order', async () => {
    setup()
    fireEvent.click(screen.getByRole('button', { name: 'Move ViberPit up' }))
    await waitFor(() => expect(reorderMutateAsync).toHaveBeenCalledTimes(2))
    expect(reorderMutateAsync.mock.calls[0]![0]).toEqual({ slug: 'viberpit', priorityRank: 1 })
    expect(reorderMutateAsync.mock.calls[1]![0]).toEqual({ slug: 'kindash', priorityRank: 2 })
  })

  it('I5 / Ruling 10: the neighbour patch never fires when the mover patch fails', async () => {
    reorderMutateAsync.mockRejectedValueOnce(new ApiRequestError(409, { detail: 'conflict' }))
    setup()
    fireEvent.click(screen.getByRole('button', { name: 'Move ViberPit up' }))
    await waitFor(() => expect(reorderMutateAsync).toHaveBeenCalledTimes(1))
    // Give any (incorrect) second call a chance to fire before asserting it never does.
    await new Promise((r) => setTimeout(r, 20))
    expect(reorderMutateAsync).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(screen.getByText(/Failed:/)).toBeInTheDocument())
  })

  it('two independent usePatchAutomation() instances back the push-level radio and the governor-mode select', () => {
    // The component itself creates two hook instances (see its own comment);
    // a shared mock can't tell them apart by call position (see `setup`'s
    // comment), so this is the honest form of that claim in this harness.
    setup()
    // Exactly two calls per render pass (`useSettingsForm`'s render-time pin-
    // form seeding makes React restart this component's render once before
    // settling, so the raw count here is a multiple of 2, not exactly 2).
    const calls = vi.mocked(hooks.usePatchAutomation).mock.calls.length
    expect(calls).toBeGreaterThanOrEqual(2)
    expect(calls % 2).toBe(0)
  })

  it('push-level descriptions come from the wire, not a client constant', () => {
    setup({ readout: { ...CAPACITY, reservePctByLevel: { conserve: 45, steady: 25, max: 7 } } })
    expect(screen.getByText('45% weekly reserve')).toBeInTheDocument()
    expect(screen.getByText('25% weekly reserve')).toBeInTheDocument()
    expect(screen.getByText('7% weekly reserve')).toBeInTheDocument()
  })

  it('the push-level radio patches the governor config', () => {
    setup()
    fireEvent.click(screen.getByRole('radio', { name: 'Max' }))
    expect(patchMutate).toHaveBeenCalledWith({ id: 'capacity-governor', body: { config: { push_level: 'max' } } })
  })

  it('SPEC: shows Saving… while a governor patch is pending', () => {
    setup({ patchState: mutationResult({ mutate: patchMutate, status: 'pending', isPending: true }) })
    // Both controls share the mocked state here (see `setup`'s comment) —
    // each instance independently renders its own RowStatus from whatever
    // `usePatchAutomation()` gave it, so both show it.
    expect(screen.getAllByText('Saving…').length).toBeGreaterThan(0)
  })

  it('SPEC: shows Failed: <message> on error, and the radio keeps the server value rather than reverting to some local guess', () => {
    setup({
      patchState: mutationResult({
        mutate: patchMutate, status: 'error', isError: true,
        error: new ApiRequestError(500, { detail: { message: 'daemon unreachable' } }),
      }),
    })
    expect(screen.getAllByText('Failed: daemon unreachable').length).toBeGreaterThan(0)
    // Controlled from the readout, not local state — still 'steady'.
    expect(screen.getByRole('radio', { name: 'Steady' })).toBeChecked()
  })

  it('the governor mode select patches the mode', () => {
    setup()
    fireEvent.change(screen.getByRole('combobox', { name: 'Governor mode' }), { target: { value: 'act' } })
    expect(patchMutate).toHaveBeenCalledWith({ id: 'capacity-governor', body: { mode: 'act' } })
  })

  it('M10: the select has no separate aria-label — the visible <label> is its only name', () => {
    setup()
    expect(screen.getByLabelText('Governor mode')).toHaveAttribute('id', 'governor-mode')
    expect(screen.getByRole('combobox', { name: 'Governor mode' })).not.toHaveAttribute('aria-label')
  })

  it('SPEC: the governor mode description is visible text, not a title attribute', () => {
    setup()
    const select = screen.getByRole('combobox', { name: 'Governor mode' })
    expect(select).not.toHaveAttribute('title')
    expect(screen.getByText('Computes and publishes a plan without applying it.')).toBeInTheDocument()
  })

  it('C2: toggling cap mode never sends expectedUpdatedAt: null, and includes the stamp when the project has one', () => {
    setup({ projectData: { automationPolicyUpdatedAt: '2026-09-19T00:00:00Z' } })
    fireEvent.click(screen.getByRole('switch', { name: 'ViberPit pinned' }))
    expect(patchPolicyMutate).toHaveBeenCalledWith({
      policy: { capacity_mode: 'pinned' }, expectedUpdatedAt: '2026-09-19T00:00:00Z',
    })
  })

  it('C2: omits expectedUpdatedAt entirely (never null) when the project has no stamp yet', () => {
    setup({ projectData: { automationPolicyUpdatedAt: null } })
    fireEvent.click(screen.getByRole('switch', { name: 'ViberPit pinned' }))
    const call = patchPolicyMutate.mock.calls[0]![0]
    expect(call).toEqual({ policy: { capacity_mode: 'pinned' } })
    expect(call).not.toHaveProperty('expectedUpdatedAt')
  })

  it('C2: useProject is read for every row unconditionally (no "touched" gate)', () => {
    setup()
    expect(hooks.useProject).toHaveBeenCalledWith('kindash', { poll: false })
    expect(hooks.useProject).toHaveBeenCalledWith('viberpit', { poll: false })
  })

  it('the plan card lists the plan reasons', () => {
    setup()
    for (const reason of CAPACITY.plan!.reasons) {
      expect(screen.getByText(reason)).toBeInTheDocument()
    }
  })

  // These three used to assert the plan card's own "This plan is stale"
  // banner; that banner is gone (governorNotice.ts now owns the one
  // sentence, single-sourced — see the component's comment), so they
  // retarget to governorNotice's title "The last plan is stale" and force
  // `governorMode: 'act'` to reach the staleness branch (the fixture's
  // default 'monitor' mode would otherwise short-circuit to its own notice).
  it('shows no stale-plan notice when the plan is only proposed (not applied)', () => {
    setup({ readout: { ...CAPACITY, governorMode: 'act' } })
    expect(screen.queryByText('The last plan is stale')).not.toBeInTheDocument()
  })

  it('shows a stale-plan notice when an applied plan is no longer live', () => {
    setup({ readout: { ...CAPACITY, governorMode: 'act', planLive: false, plan: { ...CAPACITY.plan!, applied: true } } })
    expect(screen.getByText('The last plan is stale')).toBeInTheDocument()
    expect(screen.getByText(/is using stored caps until the governor ticks again/)).toBeInTheDocument()
  })

  it('shows no stale-plan notice when an applied plan is live', () => {
    setup({ readout: { ...CAPACITY, governorMode: 'act', planLive: true, plan: { ...CAPACITY.plan!, applied: true } } })
    expect(screen.queryByText('The last plan is stale')).not.toBeInTheDocument()
  })

  it('surfaces the governor state above the stack, and one Accounts expansion per account (C18, C21)', () => {
    setup({ readout: { ...CAPACITY, killSwitch: false } })
    // `getByRole('status', { name })` can't resolve Banner's accessible name
    // in this jsdom (the title is a plain <p>, not aria-labelledby) — see
    // governorNotice's own test for the tone/title/body contract; this just
    // confirms the notice renders in the component.
    expect(screen.getByText('Governor kill switch is off')).toBeInTheDocument()
    const accounts = screen.getByTestId('capacity-stack-accounts')
    const details = within(accounts).getByRole('group')
    expect(within(details).getByText('primary — available')).toBeInTheDocument()
    expect(screen.getByRole('table', { name: 'Products' })).toHaveAttribute('id', 'products')
    expect(screen.getByRole('link', { name: 'Products' })).toHaveAttribute('href', '#products')
  })

  it('the Products link scrolls the in-page table into view rather than navigating (spec 2026-09-20 D10, F2)', () => {
    setup()
    const scrollIntoView = vi.fn()
    Element.prototype.scrollIntoView = scrollIntoView
    fireEvent.click(screen.getByRole('link', { name: 'Products' }))
    expect(scrollIntoView).toHaveBeenCalledTimes(1)
  })

  it('T4: the Accounts row still expands when it is the binding row', () => {
    setup({
      readout: {
        ...CAPACITY,
        binding: { layer: 'account', code: 'no_account_headroom', text: 'no account has headroom under the 20% reserve' },
      },
    })
    const accounts = screen.getByTestId('capacity-stack-accounts')
    expect(within(accounts).getByText('no account has headroom under the 20% reserve')).toBeInTheDocument()
    const details = within(within(accounts).getByRole('group'))
    expect(details.getByText('primary — available')).toBeInTheDocument()
  })

  it('I6: the pin input has min=0 and step=1', () => {
    setup()
    const pin = screen.getByLabelText('ViberPit pin')
    expect(pin).toHaveAttribute('min', '0')
    expect(pin).toHaveAttribute('step', '1')
  })

  it('Ruling 12: editing a pin dirties the ONE SaveBar; there is no per-row Save button', () => {
    setup()
    // The ONE SaveBar's Save starts disabled (not dirty) — this is what
    // distinguishes it from a per-row Save button, which the products table
    // no longer has at all (only one "Save" exists on the whole page).
    expect(screen.getAllByRole('button', { name: 'Save' })).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('KinDash pin'), { target: { value: '3' } })
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument()
  })

  it('Ruling 12: saving issues one policy patch per dirty row, carrying the cached project stamp, then stops', async () => {
    const { qc } = setup()
    qc.setQueryData(QK.project('kindash'), { automationPolicyUpdatedAt: 'stamp-kindash' })
    fireEvent.change(screen.getByLabelText('KinDash pin'), { target: { value: '5' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(updateProductPolicy).toHaveBeenCalledTimes(1))
    expect(updateProductPolicy).toHaveBeenCalledWith('kindash', {
      policy: { max_in_flight: 5 }, expectedUpdatedAt: 'stamp-kindash',
    })
    await waitFor(() => expect(screen.getByText(/Saved/)).toBeInTheDocument())
  })

  it('Fix round 2 / item 1: a successful pin save caches the fresh project, so a second save on the same row uses the new stamp', async () => {
    const { qc } = setup()
    qc.setQueryData(QK.project('kindash'), { automationPolicyUpdatedAt: 'stamp-1' })
    updateProductPolicy.mockResolvedValueOnce({ slug: 'kindash', automationPolicyUpdatedAt: 'stamp-2' })
    fireEvent.change(screen.getByLabelText('KinDash pin'), { target: { value: '5' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(updateProductPolicy).toHaveBeenCalledTimes(1))
    expect(qc.getQueryData(QK.project('kindash'))).toEqual({ slug: 'kindash', automationPolicyUpdatedAt: 'stamp-2' })

    updateProductPolicy.mockResolvedValueOnce({ slug: 'kindash', automationPolicyUpdatedAt: 'stamp-3' })
    fireEvent.change(screen.getByLabelText('KinDash pin'), { target: { value: '6' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(updateProductPolicy).toHaveBeenCalledTimes(2))
    expect(updateProductPolicy).toHaveBeenNthCalledWith(2, 'kindash', {
      policy: { max_in_flight: 6 }, expectedUpdatedAt: 'stamp-2',
    })
  })

  it('Fix round 2 / item 1: a 409 during a pin save invalidates the project cache before the form surfaces the failure', async () => {
    const { qc } = setup()
    qc.setQueryData(QK.project('kindash'), { automationPolicyUpdatedAt: 'stale-stamp' })
    updateProductPolicy.mockRejectedValueOnce(new ApiRequestError(409, { detail: 'conflict' }))
    fireEvent.change(screen.getByLabelText('KinDash pin'), { target: { value: '5' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.getByText('conflict')).toBeInTheDocument())
    expect(qc.getQueryState(QK.project('kindash'))?.isInvalidated).toBe(true)
  })

  it('Fix round 2 / item 2: clearing the pin field is a validation error, never a silent max_in_flight: 0', async () => {
    setup()
    fireEvent.change(screen.getByLabelText('KinDash pin'), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.getByText(/pin must be a whole number/)).toBeInTheDocument())
    expect(updateProductPolicy).not.toHaveBeenCalled()
  })

  it('Fix round 2 / item 2: a whitespace-only pin field is also a validation error', async () => {
    setup()
    fireEvent.change(screen.getByLabelText('KinDash pin'), { target: { value: '   ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.getByText(/pin must be a whole number/)).toBeInTheDocument())
    expect(updateProductPolicy).not.toHaveBeenCalled()
  })

  it('Ruling 12 / I6: an invalid pin value surfaces a form-level error instead of saving', async () => {
    setup()
    fireEvent.change(screen.getByLabelText('KinDash pin'), { target: { value: '-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.getByText(/pin must be a whole number/)).toBeInTheDocument())
    expect(updateProductPolicy).not.toHaveBeenCalled()
  })

  it('Ruling 12: the pin input is disabled while the product is auto (not pinned)', () => {
    setup()
    expect(screen.getByLabelText('ViberPit pin')).toBeDisabled()
    expect(screen.getByLabelText('KinDash pin')).toBeEnabled()
  })

  it('the runner form seeds from the automation configs and sits in the one form (spec 2026-09-20 §5.2 item 3)', () => {
    setup()
    expect(screen.getByLabelText('Runner ceiling')).toHaveValue('5')
    expect(screen.getByLabelText('Daily budget')).toHaveValue('10')
    expect(screen.getByLabelText('Sessions per account')).toHaveValue('2')
    expect(screen.getAllByRole('button', { name: 'Save' })).toHaveLength(1)
    expect(document.getElementById('runner')).not.toBeNull()
  })

  it('lowering the ceiling saves runner-first without a confirmation', async () => {
    setup()
    fireEvent.change(screen.getByLabelText('Runner ceiling'), { target: { value: '4' } })
    fireEvent.change(screen.getByLabelText('KinDash pin'), { target: { value: '3' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(updateProductPolicy).toHaveBeenCalled())
    expect(patchMutateAsync).toHaveBeenCalledWith({ id: 'backlog-runner', body: { config: { max_concurrent: 4 } } })
    expect(screen.queryByRole('dialog')).toBeNull()
    const runnerCall = patchMutateAsync.mock.invocationCallOrder[0]
    const pinCall = updateProductPolicy.mock.invocationCallOrder[0]
    expect(runnerCall).toBeLessThan(pinCall)
  })

  it('raising the ceiling asks for confirmation, then saves', async () => {
    setup()
    fireEvent.change(screen.getByLabelText('Runner ceiling'), { target: { value: '8' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/8/)).toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Raise the ceiling' }))
    await waitFor(() => expect(patchMutateAsync).toHaveBeenCalledWith({ id: 'backlog-runner', body: { config: { max_concurrent: 8 } } }))
  })

  it('a ceiling above the global cap is refused before anything is sent', async () => {
    setup()
    fireEvent.change(screen.getByLabelText('Runner ceiling'), { target: { value: '21' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('Runner ceiling must not exceed the global cap (20)')).toBeInTheDocument()
    expect(patchMutateAsync).not.toHaveBeenCalled()
  })

  it('sessions per account patches the governor', async () => {
    setup()
    fireEvent.change(screen.getByLabelText('Sessions per account'), { target: { value: '3' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(patchMutateAsync).toHaveBeenCalledWith({ id: 'capacity-governor', body: { config: { sessions_per_account: 3 } } }))
  })

  it('with no Backlog Runner registered the ceiling and budget fields are disabled and nothing is sent', async () => {
    setup({ automations: [GOVERNOR] })
    expect(screen.getByLabelText('Runner ceiling')).toBeDisabled()
    expect(screen.getByLabelText('Daily budget')).toBeDisabled()
    expect(screen.getByLabelText('Sessions per account')).not.toBeDisabled()
  })
})
