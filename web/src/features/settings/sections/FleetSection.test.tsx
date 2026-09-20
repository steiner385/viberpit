/**
 * FleetSection — the per-host Codex column.
 *
 * The invariant that matters most here is a NEGATIVE one: a Codex mismatch must
 * leave host health and Claude dispatch completely alone. A runner with no
 * Codex binary is a perfectly good Claude runner, and a fleet table that turns
 * it red would send an operator to fix the wrong thing.
 *
 * The tri-state convention is `healthLabel`'s: absent/null is "not advertised",
 * never "unavailable".
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'

import { FleetSection } from './FleetSection'
import { api } from '@/api/client'
import * as fleet from '@/api/fleet'
import * as hooks from '@/api/hooks'
import type { FleetHost, HostEngineState } from '@/api/fleet'
import { fleetHost, appSettings } from '@/test/factories'
import { queryResult } from '@/test/reactQuery'

// Spread `actual` (not a bare stub): `FleetSection` also calls the real
// `useFleetHostPatch`, which this file needs working (it drives `api.put`,
// mocked below) — only `usePendingDeploy` and `useSettings` are overridden here.
vi.mock('@/api/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/hooks')>()
  return {
    ...actual,
    usePendingDeploy: vi.fn(() => ({ data: undefined })),
    useSettings: vi.fn(),
  }
})

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { ...actual, api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), del: vi.fn() } }
})

vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }))
import { toast } from 'sonner'

function engineState(over: Partial<HostEngineState> = {}): HostEngineState {
  return {
    engine: 'codex', available: true, reason: null, cliVersion: '0.9.0',
    contractSha256: 'abc123', externalAuth: true, dispatchable: true, dispatchReason: null,
    ...over,
  }
}

/** One entry per engine, as `_engines_wire` always emits. */
function engines(codex: Partial<HostEngineState>): HostEngineState[] {
  return [engineState({ engine: 'claude_code' }), engineState({ engine: 'codex', ...codex })]
}

function renderFleet(hosts: FleetHost[]) {
  vi.spyOn(fleet, 'useFleetHosts').mockReturnValue(
    queryResult({ hosts }) as ReturnType<typeof fleet.useFleetHosts>)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createMemoryRouter([{ path: '/', element: <FleetSection /> }])
  return render(
    <QueryClientProvider client={qc}><RouterProvider router={router} /></QueryClientProvider>,
  )
}

/** The table row for a host, so a per-host cell can be read without ambiguity. */
function row(name: string): HTMLElement {
  return screen.getByText(name).closest('tr') as HTMLElement
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings()))
})

describe('FleetSection — the Codex column', () => {
  it('has a Codex column header', () => {
    renderFleet([fleetHost({ name: 'edge', engines: engines({}) })])
    expect(screen.getByRole('columnheader', { name: 'Codex' })).toBeInTheDocument()
  })

  it('says "dispatchable" for a host the edge will send Codex to', () => {
    renderFleet([fleetHost({ name: 'edge', engines: engines({}) })])
    expect(within(row('edge')).getByText('dispatchable')).toBeInTheDocument()
  })

  it('says "not advertised" — never "unavailable" — for a legacy runner', () => {
    // Tri-state, exactly like `healthy: null`: the runner predates /info.engines,
    // which is not the same as telling us it cannot run Codex.
    renderFleet([
      fleetHost({ name: 'old', engines: undefined }),
      fleetHost({ name: 'older', engines: engines({ available: false, reason: 'engine_not_advertised', dispatchable: false, dispatchReason: 'engine_not_advertised' }) }),
    ])
    expect(within(row('old')).getByText(/not advertised/)).toBeInTheDocument()
    expect(within(row('older')).getByText(/not advertised/)).toBeInTheDocument()
    expect(within(row('old')).queryByText(/^unavailable$/)).not.toBeInTheDocument()
  })

  it('shows the dispatchReason when the edge refuses', () => {
    renderFleet([fleetHost({
      name: 'rocky',
      engines: engines({ available: false, reason: 'binary_not_found', dispatchable: false, dispatchReason: 'binary_not_found' }),
    })])
    expect(within(row('rocky')).getByText(/agent binary not installed/)).toBeInTheDocument()
  })

  it('distinguishes "the runner can, but this edge will not" from "the runner cannot"', () => {
    // available: true + dispatchable: false is a real state — a runner running
    // Codex against a schema contract this edge is not pinned to. Which half is
    // at fault decides which machine gets upgraded.
    renderFleet([fleetHost({
      name: 'rocky',
      engines: engines({ available: true, reason: null, dispatchable: false, dispatchReason: 'contract_hash_mismatch' }),
    })])
    const cell = within(row('rocky'))
    expect(cell.getByText(/runner ready/i)).toBeInTheDocument()
    expect(cell.getByText(/app-server contract differs from this edge/)).toBeInTheDocument()
  })

  it('leaves host health untouched when Codex is broken', () => {
    renderFleet([fleetHost({
      name: 'rocky',
      healthy: true,
      engines: engines({ available: false, reason: 'binary_not_found', dispatchable: false, dispatchReason: 'binary_not_found' }),
    })])
    expect(within(row('rocky')).getByText('healthy')).toBeInTheDocument()
  })

  it('still reports Codex on a host that has never been probed', () => {
    renderFleet([fleetHost({ name: 'fresh', healthy: null, engines: undefined })])
    expect(within(row('fresh')).getByText('not yet probed')).toBeInTheDocument()
    expect(within(row('fresh')).getByText(/not advertised/)).toBeInTheDocument()
  })
})

// Task 9 (spec D9): two "weight" dials exist in this app — this one is the
// per-host DISPATCH-share multiplier, the product policy card's is a
// per-product CONTESTED-SLOT share. The label disambiguates them; the Cap
// column is the editable per-host cap alongside it.
describe('FleetSection — weight and cap', () => {
  it('labels the weight column and input as the dispatch-share weight', () => {
    renderFleet([fleetHost({ name: 'rocky', weight: 3 })])
    expect(screen.getByRole('columnheader', { name: 'Weight (dispatch share)' })).toBeInTheDocument()
    expect(screen.getByLabelText('weight (dispatch share) for rocky')).toHaveValue(3)
  })

  it('patches weight on blur when it changed', async () => {
    renderFleet([fleetHost({ name: 'rocky', weight: 3 })])
    const input = screen.getByLabelText('weight (dispatch share) for rocky')
    fireEvent.change(input, { target: { value: '5' } })
    fireEvent.blur(input)
    await waitFor(() => {
      expect(api.put).toHaveBeenCalledWith('/fleet/hosts/rocky', { weight: 5 })
    })
  })

  it('does not patch weight on blur when it is unchanged', () => {
    renderFleet([fleetHost({ name: 'rocky', weight: 3 })])
    fireEvent.blur(screen.getByLabelText('weight (dispatch share) for rocky'))
    expect(api.put).not.toHaveBeenCalled()
  })

  it('has a Cap column, blank for a host with no cap', () => {
    renderFleet([fleetHost({ name: 'rocky', maxSessions: null })])
    expect(screen.getByRole('columnheader', { name: 'Cap' })).toBeInTheDocument()
    expect(screen.getByLabelText('cap for rocky')).toHaveValue('')
  })

  it('patches cap on blur with a number', async () => {
    renderFleet([fleetHost({ name: 'rocky', maxSessions: null })])
    const input = screen.getByLabelText('cap for rocky')
    fireEvent.change(input, { target: { value: '4' } })
    fireEvent.blur(input)
    await waitFor(() => {
      expect(api.put).toHaveBeenCalledWith('/fleet/hosts/rocky', { maxSessions: 4 })
    })
  })

  it('clearing the field patches cap back to null', async () => {
    renderFleet([fleetHost({ name: 'rocky', maxSessions: 4 })])
    const input = screen.getByLabelText('cap for rocky')
    fireEvent.change(input, { target: { value: '' } })
    fireEvent.blur(input)
    await waitFor(() => {
      expect(api.put).toHaveBeenCalledWith('/fleet/hosts/rocky', { maxSessions: null })
    })
  })

  it('does not patch cap on blur when it is unchanged', () => {
    renderFleet([fleetHost({ name: 'rocky', maxSessions: 4 })])
    fireEvent.blur(screen.getByLabelText('cap for rocky'))
    expect(api.put).not.toHaveBeenCalled()
  })

  // Fix wave item 1: a non-empty, non-whole-number-≥1 value must never reach
  // the server — it used to swallow the failure entirely (a 400 nobody saw).
  it('"abc" sends nothing, reverts the field, and shows a row-level error', () => {
    renderFleet([fleetHost({ name: 'rocky', maxSessions: 4 })])
    const input = screen.getByLabelText('cap for rocky')
    fireEvent.change(input, { target: { value: 'abc' } })
    fireEvent.blur(input)
    expect(api.put).not.toHaveBeenCalled()
    expect(input).toHaveValue('4')
    expect(screen.getByRole('alert')).toHaveTextContent('Cap must be a whole number ≥ 1')
  })

  it('"0" sends nothing, reverts the field, and shows a row-level error', () => {
    renderFleet([fleetHost({ name: 'rocky', maxSessions: 4 })])
    const input = screen.getByLabelText('cap for rocky')
    fireEvent.change(input, { target: { value: '0' } })
    fireEvent.blur(input)
    expect(api.put).not.toHaveBeenCalled()
    expect(input).toHaveValue('4')
    expect(screen.getByRole('alert')).toHaveTextContent('Cap must be a whole number ≥ 1')
  })

  it('a valid blur after an error clears the row-level error', async () => {
    renderFleet([fleetHost({ name: 'rocky', maxSessions: 4 })])
    const input = screen.getByLabelText('cap for rocky')
    fireEvent.change(input, { target: { value: '0' } })
    fireEvent.blur(input)
    expect(screen.getByRole('alert')).toBeInTheDocument()
    fireEvent.change(input, { target: { value: '7' } })
    fireEvent.blur(input)
    await waitFor(() => expect(api.put).toHaveBeenCalledWith('/fleet/hosts/rocky', { maxSessions: 7 }))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  // Ruling: a rejected PATCH must never vanish silently — the API error
  // reaches the operator as a toast (the accompanying re-invalidation is
  // covered where `useFleetHosts` is the REAL hook — see
  // `FleetSection.hostTable.test.tsx`, which can actually observe a refetch;
  // this file mocks `useFleetHosts` outright, so a cache invalidation here
  // would have nothing to act on).
  it('a rejected cap patch toasts the API error', async () => {
    vi.mocked(api.put).mockRejectedValueOnce({ body: { detail: { message: 'host not found' } } })
    renderFleet([fleetHost({ name: 'rocky', maxSessions: 4 })])
    const input = screen.getByLabelText('cap for rocky')
    fireEvent.change(input, { target: { value: '7' } })
    fireEvent.blur(input)
    await waitFor(() => expect(toast.error).toHaveBeenCalled())
  })

  it('has one heading (the chassis h2), not a second "Fleet" h3 (spec 2026-09-20 C2)', () => {
    renderFleet([fleetHost({ name: 'rocky' })])
    expect(screen.queryByRole('heading', { level: 3, name: 'Fleet' })).toBeNull()
  })

  it('footer states the physical ceiling and links to Limits', () => {
    vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings({
      effectiveDefaults: {
        repoRoot: '/r', archiveHome: null, modelApprovalAction: 'require_approval', modelApprovalDenylist: 'fable',
        globalCap: { physical: 10, attention: null, effective: 10, binding: 'physical', hosts: [{ name: 'dobby', cap: 4 }, { name: 'rocky', cap: 6 }] },
      },
    })))
    renderFleet([fleetHost({ name: 'rocky' })])
    expect(screen.getByText(/Σ caps = 10/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'physical ceiling' })).toHaveAttribute('href', '/settings/capacity')
  })
})
