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
  configSchema: [], config: {}, schedule: null, lastActiveAt: null, activeSessions: 0,
  oversight: {
    responsibility: { id: 'execution', label: 'Execute and finish work' },
    scope: 'Fleet capacity', modeDescriptions: { monitor: 'Computes and publishes a plan without applying it.' },
    relationships: [],
  },
}

const patchMutate = vi.fn()
const reorderMutateAsync = vi.fn().mockResolvedValue({})
const patchPolicyMutate = vi.fn()

function setup({
  patchState = mutationResult({ mutate: patchMutate }),
  patchPolicyState = mutationResult({ mutate: patchPolicyMutate }),
  projectData,
}: {
  patchState?: ReturnType<typeof mutationResult>
  patchPolicyState?: ReturnType<typeof mutationResult>
  projectData?: { automationPolicyUpdatedAt: string | null }
} = {}) {
  vi.mocked(hooks.useCapacity).mockReturnValue(queryResult(CAPACITY) as never)
  vi.mocked(hooks.useAutomations).mockReturnValue(queryResult([GOVERNOR]) as never)
  // `usePatchAutomation()` is called twice per render (once for the
  // push-level radio, once for the governor-mode select — two SEPARATE hook
  // instances so each shows its own Saving/Saved/Failed status in the real
  // component). A `vi.fn()` mock has no notion of "hook call position" the
  // way React's real hook slots do, and `useSettingsForm`'s render-time state
  // adjustment (seeding the pin form) makes React restart this component's
  // render at least once before settling — so a `mockReturnValueOnce` chain
  // keyed to "1st call = push level, 2nd = governor" silently drifts onto the
  // wrong control once that restart consumes the first two slots. One shared
  // state for both is the fixture this file can make honestly; the
  // independence itself is what `usePatchAutomation` being called twice
  // (asserted below) stands for.
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
})
