/**
 * `useRoadmapWorkspace`'s two transport decisions, tested at the HOOK rather
 * than through the shell.
 *
 * The shell-level sibling (`shell/roadmapWorkspace.test.tsx`) cannot cover
 * either of them for the triage queue: the Roadmap nav badge polls the unscoped
 * `[...QK.triage, null]` key on every route, so mounting the app makes that
 * exact request whatever this hook does, and react-query dedupes a second
 * observer of a live key into zero extra fetches. Rendering the hook ALONE is
 * what makes its own requests visible:
 *
 * 1. `active: false` — the `enabled` gate. Inactive, it must ask for nothing.
 * 2. `poll` — the unscoped queue rides the badge's interval (`poll: false`), a
 *    `?product=` scope is its own key with no other observer and polls itself.
 *
 * Both are invisible to the component (a disabled query and an un-fetched one
 * both read `data: undefined`), so the api CLIENT is faked and the real hooks
 * run against it — the arrangement `roadmapWorkspace.test.tsx` uses.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  const { MAP } = await import('@/features/roadmap/__fixtures__/map')
  return {
    ...actual,
    api: {
      ...actual.api,
      get: vi.fn(async (path: string): Promise<unknown> => (path === '/pm/map' ? MAP : [])),
    },
  }
})

import { api } from '@/api/client'
import { useRoadmapWorkspace } from './useRoadmapWorkspace'

const get = vi.mocked(api.get)
const paths = () => get.mock.calls.map(([p]) => p)
const triageCalls = () => paths().filter((p) => p.startsWith('/pm/triage'))
const decisionCalls = () => paths().filter((p) => p.startsWith('/pm/decisions'))

function wrap(initial: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initial]}>{children}</MemoryRouter>
    </QueryClientProvider>
  )
}

describe('useRoadmapWorkspace transport', () => {
  beforeEach(() => get.mockClear())

  it('asks for nothing at all while the column is not the section (active: false)', async () => {
    renderHook(() => useRoadmapWorkspace(false), { wrapper: wrap('/sessions') })
    // Settle: give both queries every chance to fire before concluding they did
    // not. An `enabled: false` query never resolves, so there is nothing to wait
    // ON — a real interval tick is the only honest settle point here.
    await act(async () => { await new Promise((r) => setTimeout(r, 20)) })
    expect(paths()).toEqual([])
  })

  it('fetches map and queue once the column IS the section', async () => {
    renderHook(() => useRoadmapWorkspace(true), { wrapper: wrap('/roadmap?scope=inbox') })
    await waitFor(() => expect(paths()).toContain('/pm/map'))
    await waitFor(() => expect(triageCalls()).toEqual(['/pm/triage']))
  })
})

describe('useRoadmapWorkspace polling', () => {
  beforeEach(() => {
    get.mockClear()
    vi.useFakeTimers()
  })
  afterEach(() => vi.useRealTimers())

  /** Mount, let the first round of fetches land, then run four poll intervals. */
  async function requestsAfterFourIntervals(initial: string) {
    renderHook(() => useRoadmapWorkspace(true), { wrapper: wrap(initial) })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(triageCalls().length).toBe(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
    return triageCalls()
  }

  it('does not poll the UNSCOPED queue — the nav badge owns that interval', async () => {
    expect(await requestsAfterFourIntervals('/roadmap?scope=inbox')).toEqual(['/pm/triage'])
  })

  it('polls a ?product= scope, which no other observer holds', async () => {
    const calls = await requestsAfterFourIntervals('/roadmap?scope=inbox&product=kindash')
    expect(calls.length).toBeGreaterThan(1)
    expect(new Set(calls)).toEqual(new Set(['/pm/triage?projectSlug=kindash']))
  })
})

/**
 * The inbox's second list (spec 2026-09-18 §6), scoped server-side: the
 * `?product=` chip becomes `projectSlug=` and the column always asks for impact.
 */
describe('useRoadmapWorkspace — decision proposals', () => {
  const D = (over: Record<string, unknown>) => ({
    id: 'd1', workItemId: 'w9', repo: 'cairnea/KinDash', number: 12,
    ref: 'cairnea/KinDash#12', projectSlug: 'kindash', title: 'Queue or stream?',
    url: null, options: [{ key: 'a', label: 'A' }, { key: 'b', label: 'B' }],
    recommendedKey: 'a', rationale: null, chosenKey: null, status: 'pending',
    proposedBy: 'session:s1', resolvedBy: null, createdAt: null, resolvedAt: null,
    ...over,
  })

  beforeEach(() => get.mockClear())
  afterEach(() => {
    vi.useRealTimers()
    // The per-test implementations above outlive their test otherwise.
    get.mockReset()
    get.mockImplementation(async (path?: string): Promise<unknown> => {
      const { MAP } = await import('@/features/roadmap/__fixtures__/map')
      return path === '/pm/map' ? MAP : []
    })
  })

  it('asks for nothing while the column is not the section', async () => {
    renderHook(() => useRoadmapWorkspace(false), { wrapper: wrap('/sessions') })
    await act(async () => { await new Promise((r) => setTimeout(r, 20)) })
    expect(decisionCalls()).toEqual([])
  })

  it('polls the unscoped list with impact — the badge observes a different key', async () => {
    vi.useFakeTimers()
    renderHook(() => useRoadmapWorkspace(true), { wrapper: wrap('/roadmap?scope=inbox') })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
    const calls = decisionCalls()
    expect(calls.length).toBeGreaterThan(1)
    expect(new Set(calls)).toEqual(new Set(['/pm/decisions?impact=true']))
  })

  it('concatenates both kinds into ONE queue', async () => {
    get.mockImplementation(async (path?: string): Promise<unknown> => {
      if (path === '/pm/decisions?impact=true') return [D({ id: 'd1' })]
      const { MAP } = await import('@/features/roadmap/__fixtures__/map')
      return path === '/pm/map' ? MAP : []
    })
    const { result } = renderHook(() => useRoadmapWorkspace(true), { wrapper: wrap('/roadmap?scope=inbox') })
    await waitFor(() => expect(result.current.list.inbox.proposals).toHaveLength(1))
    expect(result.current.list.inbox.proposals[0]).toMatchObject({ id: 'd1', proposalKind: 'decision' })
  })

  it('scopes to the ?product= chip on the server and does no client-side filtering', async () => {
    get.mockImplementation(async (path?: string): Promise<unknown> => {
      if (path === '/pm/decisions?projectSlug=kindash&impact=true') {
        // The server owns the narrowing: a row for another product that slips
        // through is rendered, not re-filtered.
        return [D({ id: 'mine' }), D({ id: 'other', projectSlug: 'viberpit' })]
      }
      const { MAP } = await import('@/features/roadmap/__fixtures__/map')
      return path === '/pm/map' ? MAP : []
    })
    const { result } = renderHook(
      () => useRoadmapWorkspace(true), { wrapper: wrap('/roadmap?scope=inbox&product=kindash') })
    await waitFor(() => expect(result.current.list.inbox.proposals).toHaveLength(2))
    expect(decisionCalls()).toEqual(['/pm/decisions?projectSlug=kindash&impact=true'])
  })
})
