/**
 * `useDeliveryWorkspace`'s two transport decisions, tested at the HOOK rather
 * than through the shell — the sibling of
 * `features/roadmap/useRoadmapWorkspace.test.tsx`, and for the same reason.
 *
 * The shell-level view cannot see either of them for the automation list: the
 * Delivery nav badge reads `QK.automations` on every route, so mounting the app
 * makes that exact request whatever this hook does, and react-query dedupes a
 * second observer of a live key into zero extra fetches. Rendering the hook
 * ALONE is what makes its own requests visible:
 *
 * 1. `active: false` — the `enabled` gate on `/pipeline/repos`. Inactive, the
 *    CI scope's repo set must not be fetched at all.
 * 2. `poll` — the CI detail (`useStageModels` in `DeliveryPage`) owns no timer
 *    either, so the column's copy must not add one: one fetch, no interval.
 *
 * `/automations` is the deliberate exception: it is the badge's key, already
 * polled app-wide, so the column reads it ungated in both cases.
 *
 * Both are invisible to the component (a disabled query and an un-fetched one
 * both read `data: undefined`), so the api CLIENT is faked and the real hooks
 * run against it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  const { AUTOMATIONS } = await import('./__fixtures__/automations')
  return {
    ...actual,
    api: {
      ...actual.api,
      get: vi.fn(async (path: string): Promise<unknown> => {
        if (path === '/automations') return AUTOMATIONS
        if (path === '/pipeline/repos') return { 'o/r': { repo: 'o/r', nodes: [] } }
        return []
      }),
    },
  }
})

import { api } from '@/api/client'
import { useDeliveryWorkspace } from './useDeliveryWorkspace'

const get = vi.mocked(api.get)
const paths = () => get.mock.calls.map(([p]) => p)
const repoCalls = () => paths().filter((p) => p === '/pipeline/repos')
const automationCalls = () => paths().filter((p) => p === '/automations')

function wrap(initial: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initial]}>{children}</MemoryRouter>
    </QueryClientProvider>
  )
}

describe('useDeliveryWorkspace transport', () => {
  beforeEach(() => get.mockClear())

  it('does not fetch the repo set while the column is not the section (active: false)', async () => {
    renderHook(() => useDeliveryWorkspace(false), { wrapper: wrap('/sessions') })
    // Settle: give the query every chance to fire before concluding it did not.
    // An `enabled: false` query never resolves, so there is nothing to wait ON —
    // a real interval tick is the only honest settle point here.
    await act(async () => { await new Promise((r) => setTimeout(r, 20)) })
    expect(repoCalls()).toEqual([])
  })

  it('reads the automation list even while inactive — it is the nav badge\'s key', async () => {
    renderHook(() => useDeliveryWorkspace(false), { wrapper: wrap('/sessions') })
    await waitFor(() => expect(automationCalls()).toEqual(['/automations']))
  })

  it('fetches the repo set once the column IS the section', async () => {
    renderHook(() => useDeliveryWorkspace(true), { wrapper: wrap('/delivery?scope=ci') })
    await waitFor(() => expect(repoCalls()).toEqual(['/pipeline/repos']))
  })
})

describe('useDeliveryWorkspace polling', () => {
  beforeEach(() => {
    get.mockClear()
    vi.useFakeTimers()
  })
  afterEach(() => vi.useRealTimers())

  it('does not poll the repo set — the CI detail owns that key', async () => {
    renderHook(() => useDeliveryWorkspace(true), { wrapper: wrap('/delivery?scope=ci') })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(repoCalls()).toEqual(['/pipeline/repos'])
    // Past `useStageModels`' 30 s interval: a second call here means the column
    // opened a timer over a key it does not own.
    await act(async () => { await vi.advanceTimersByTimeAsync(31_000) })
    expect(repoCalls()).toEqual(['/pipeline/repos'])
  })
})

describe('useDeliveryWorkspace list props', () => {
  beforeEach(() => get.mockClear())

  // The row keys (`selectedKey`) need real route params, so they are asserted
  // end-to-end through the shell in `DeliveryPage.test.tsx`; what is checked
  // here is the pair the hook derives on its own — the scope the path names and
  // the repo set `/pipeline/repos` is flattened into.
  it('derives the scope from the path and the repo list from the stage models', async () => {
    const { result } = renderHook(
      () => useDeliveryWorkspace(true),
      { wrapper: wrap('/delivery/ci/o/r') },
    )
    await waitFor(() => expect(result.current.list.repos).toEqual(['o/r']))
    expect(result.current.list.scope).toBe('ci')
  })
})
