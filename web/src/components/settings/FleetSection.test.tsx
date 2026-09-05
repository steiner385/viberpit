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
import { render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { FleetSection } from './FleetSection'
import * as fleet from '@/api/fleet'
import type { FleetHost, HostEngineState } from '@/api/fleet'
import { fleetHost } from '@/test/factories'
import { queryResult } from '@/test/reactQuery'

vi.mock('@/api/hooks', () => ({
  usePendingDeploy: vi.fn(() => ({ data: undefined })),
}))

vi.mock('@/api/client', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), del: vi.fn() },
}))

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
  return render(
    <QueryClientProvider client={qc}><FleetSection /></QueryClientProvider>,
  )
}

/** The table row for a host, so a per-host cell can be read without ambiguity. */
function row(name: string): HTMLElement {
  return screen.getByText(name).closest('tr') as HTMLElement
}

beforeEach(() => vi.clearAllMocks())

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
