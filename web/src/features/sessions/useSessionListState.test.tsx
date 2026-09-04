// The Status/Project/Environment filters on the SHARED list handle (DEC-8).
//
// `useSessionListState` is called once, in `AppShell.useSessionsWorkspace()`, and
// the handle goes to both `ContextList` (→ `SessionListColumn`) and `PromptDock`
// (→ `DispatchBar`). Filter state anywhere else and the two disagree about which
// rows exist — the prompt bar's ↑/↓ would walk onto rows the list is not showing.
// So the filters are pinned HERE, on the handle, not through a rendered control.
//
// Nothing about the fleet is stubbed at the hook level: `@/api/client`'s `api` is
// the only fake, so `useFleetHosts` is the REAL hook. That matters — the
// environment domain must be the whole registry, and a test that stubbed
// `useFleetHosts` could not tell `useFleetHosts` from `useEnabledFleetHosts`
// (the disabled host below is exactly that discrimination).
import * as React from 'react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import type { Session } from '@/api/types'
import type { FleetHost } from '@/api/fleet'
import { NO_FILTERS, type ListFilters } from '@/session/listFilters'
import { nextSessionAfterClose } from '@/session/nextAfterArchive'

// ── typed factories (R10: real values, never `as unknown as T`) ───────────────

function mk(id: string, over: Partial<Session> = {}): Session {
  return {
    id, name: id, accountName: 'acct', status: 'idle',
    createdAt: '2026-06-01T00:00:00Z', updatedAt: '2026-06-01T00:00:00Z', prs: [],
    ...over,
  }
}

function fleetHost(name: string, over: Partial<FleetHost> = {}): FleetHost {
  return {
    name,
    endpoint: `http://${name}:8760`,
    enabled: true,
    weight: 1,
    maxSessions: null,
    healthy: true,
    activeSessions: 0,
    loadAvg: null,
    memAvailableMb: null,
    sha: null,
    checkoutSha: null,
    protocol: null,
    lastProbeAt: null,
    lastError: null,
    sshHost: null,
    repoPath: null,
    upgrade: null,
    ...over,
  }
}

// ── the fake daemon ──────────────────────────────────────────────────────────
//
// `hostsGate`, when set, holds `/fleet/hosts` unresolved so a test can observe
// the render where the fleet query has NOT answered yet.

let HOSTS: FleetHost[] = []
let hostsGate: Promise<void> | null = null

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      get: async (path: string): Promise<unknown> => {
        if (path === '/fleet/hosts') {
          if (hostsGate) await hostsGate
          return { hosts: HOSTS }
        }
        if (path.startsWith('/sessions/search')) {
          const q = (new URLSearchParams(path.split('?')[1] ?? '').get('q') ?? '').toLowerCase()
          return { ids: ALL.filter((s) => s.name.toLowerCase().includes(q)).map((s) => s.id) }
        }
        return {}
      },
    },
  }
})

import { useSessionListState, type SessionListStateOptions } from './useSessionListState'

// ── fixtures ─────────────────────────────────────────────────────────────────
//
// tiers: idle → 'your-turn'; in_progress → 'busy'; waiting_input → 'needs-you'.

const idleA = mk('idleA', { repo: 'o/a', host: 'dobby' })
const idleB = mk('idleB', { repo: 'o/b', host: 'rocky' })
const busyA = mk('busyA', { status: 'in_progress', repo: 'o/a', host: 'rocky' })
const askA = mk('askA', { status: 'waiting_input', repo: 'o/a', host: 'dobby' })
const scratch = mk('scratch')
const queuedA = mk('queuedA', { status: 'queued', repo: 'o/a', host: 'dobby', queuePosition: 1 })
const pendingA = mk('pendingA', { status: 'pending_approval', repo: 'o/a', host: 'dobby' })

const LIVE = [idleA, idleB, busyA, askA, scratch]
const ALL = [...LIVE, queuedA, pendingA]

const ORDER_KEY = 'ccswitch.sessionOrder'
const ids = (list: readonly Session[]): string[] => list.map((s) => s.id)
const sorted = (list: readonly Session[]): string[] => ids(list).slice().sort()
const filters = (over: Partial<ListFilters> = {}): ListFilters => ({ ...NO_FILTERS, ...over })

function storedFilters(): unknown {
  const raw = window.localStorage.getItem(ORDER_KEY)
  return raw === null ? null : (JSON.parse(raw) as { filters?: unknown }).filters
}

// ── harness ──────────────────────────────────────────────────────────────────

interface Props {
  sessions: readonly Session[]
  sessionsLoading?: boolean
}

function makeWrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  }
}

function mount(initialProps: Props) {
  return renderHook(
    ({ sessions, sessionsLoading }: Props) =>
      useSessionListState(sessions, { sessionsLoading } satisfies SessionListStateOptions),
    { initialProps, wrapper: makeWrapper() },
  )
}

/** Mount and wait until the fleet query has answered — i.e. until the hook is
 *  `ready` and therefore willing to sanitize. Every host fixture is non-empty so
 *  "the registry arrived" is observable. */
async function mountReady(sessions: readonly Session[] = ALL) {
  const h = mount({ sessions, sessionsLoading: false })
  await waitFor(() =>
    expect(h.result.current.filterOptions.environment).toHaveLength(HOSTS.length),
  )
  return h
}

beforeEach(() => {
  window.localStorage.clear()
  hostsGate = null
  // 'rocky' is DISABLED and 'edge' runs nothing: both must still be selectable.
  HOSTS = [fleetHost('dobby'), fleetHost('rocky', { enabled: false }), fleetHost('edge')]
})

// ── filtering feeds the ordering ─────────────────────────────────────────────

describe('useSessionListState filters → ordering', () => {
  it('a status filter narrows ordering.orderedSessions to that tier', async () => {
    const h = await mountReady()
    expect(sorted(h.result.current.ordering.orderedSessions))
      .toEqual(['askA', 'busyA', 'idleA', 'idleB', 'scratch'])

    act(() => h.result.current.setFilters(filters({ status: 'busy' })))
    expect(ids(h.result.current.ordering.orderedSessions)).toEqual(['busyA'])
    expect(h.result.current.ordering.groups.flatMap((g) => g.sessions).map((s) => s.id))
      .toEqual(['busyA'])
  })

  it('a project filter narrows the list and excludes repo-less rows', async () => {
    const h = await mountReady()
    act(() => h.result.current.setFilters(filters({ project: 'o/a' })))
    expect(sorted(h.result.current.ordering.orderedSessions)).toEqual(['askA', 'busyA', 'idleA'])
  })

  it('an environment filter selects a DISABLED host — the registry is the domain', async () => {
    const h = await mountReady()
    // 'rocky' is enabled:false. `useEnabledFleetHosts` would not offer it, and a
    // sanitize against those options would drop the stored value on the spot.
    expect(h.result.current.filterOptions.environment.map((o) => o.value))
      .toEqual(['dobby', 'edge', 'rocky'])
    act(() => h.result.current.setFilters(filters({ environment: 'rocky' })))
    expect(h.result.current.filters.environment).toBe('rocky')
    expect(sorted(h.result.current.ordering.orderedSessions)).toEqual(['busyA', 'idleB'])
  })

  it('filters AND together', async () => {
    const h = await mountReady()
    act(() => h.result.current.setFilters(filters({ project: 'o/a', environment: 'dobby' })))
    expect(sorted(h.result.current.ordering.orderedSessions)).toEqual(['askA', 'idleA'])
  })

  it('a zero-match filter survives rather than clearing itself', async () => {
    // 'edge' is configured but runs nothing. The list must go EMPTY and the
    // selection must stay, or the control the user would clear it with is
    // rendered from an option set that no longer explains the empty list.
    const h = await mountReady()
    act(() => h.result.current.setFilters(filters({ environment: 'edge' })))
    expect(h.result.current.ordering.orderedSessions).toEqual([])
    expect(h.result.current.filters.environment).toBe('edge')

    // Same for a tier with no members: 'error' is a closed-vocabulary value.
    act(() => h.result.current.setFilters(filters({ status: 'error' })))
    expect(h.result.current.ordering.orderedSessions).toEqual([])
    expect(h.result.current.filters.status).toBe('error')
  })
})

// ── the option lists come from the UNFILTERED set ────────────────────────────

describe('filterOptions', () => {
  it('projects come from the unfiltered sessions, so choosing one keeps the rest', async () => {
    const h = await mountReady()
    expect(h.result.current.filterOptions.project.map((o) => o.value)).toEqual(['o/a', 'o/b'])
    act(() => h.result.current.setFilters(filters({ project: 'o/a' })))
    // Derived from the filtered rows, this would collapse to ['o/a'] and the
    // user could never switch to the other project without clearing first.
    expect(h.result.current.filterOptions.project.map((o) => o.value)).toEqual(['o/a', 'o/b'])
  })

  it('environments come from the unfiltered registry, not the surviving rows', async () => {
    const h = await mountReady()
    act(() => h.result.current.setFilters(filters({ environment: 'dobby' })))
    expect(h.result.current.filterOptions.environment.map((o) => o.value))
      .toEqual(['dobby', 'edge', 'rocky'])
  })

  it('status stays the full closed vocabulary under any filter', async () => {
    const h = await mountReady()
    const all = h.result.current.filterOptions.status.map((o) => o.value)
    expect(all).toEqual(['needs-you', 'unreachable', 'error', 'your-turn', 'waiting', 'busy'])
    act(() => h.result.current.setFilters(filters({ status: 'busy' })))
    expect(h.result.current.filterOptions.status.map((o) => o.value)).toEqual(all)
  })
})

// ── the DEC-3 pre-spawn partition still applies ──────────────────────────────

describe('pre-spawn partition (DEC-3)', () => {
  it('queued/pending_approval rows never enter the grouped list, filtered or not', async () => {
    const h = await mountReady()
    expect(ids(h.result.current.queued)).toEqual(['queuedA'])
    expect(ids(h.result.current.pendingApproval)).toEqual(['pendingA'])
    expect(sorted(h.result.current.ordering.orderedSessions))
      .toEqual(['askA', 'busyA', 'idleA', 'idleB', 'scratch'])

    // queuedA/pendingA carry repo 'o/a' and host 'dobby'; a filter matching both
    // must still not pull them into the grouped list.
    act(() => h.result.current.setFilters(filters({ project: 'o/a', environment: 'dobby' })))
    expect(sorted(h.result.current.ordering.orderedSessions)).toEqual(['askA', 'idleA'])
    // …and their own sections are untouched (documented on the hook). Filter to
    // a project NEITHER pre-spawn row belongs to: they would vanish if the same
    // predicate ran over those sections, which is the behaviour this pins.
    act(() => h.result.current.setFilters(filters({ project: 'o/b' })))
    expect(ids(h.result.current.ordering.orderedSessions)).toEqual(['idleB'])
    expect(ids(h.result.current.queued)).toEqual(['queuedA'])
    expect(ids(h.result.current.pendingApproval)).toEqual(['pendingA'])

    // A status filter is the sharper case: 'queued'/'pending_approval' have no
    // tier of their own, so ANY tier selection would empty both sections.
    act(() => h.result.current.setFilters(filters({ status: 'busy' })))
    expect(ids(h.result.current.queued)).toEqual(['queuedA'])
    expect(ids(h.result.current.pendingApproval)).toEqual(['pendingA'])
  })

  it('a pre-spawn row is not offered as a project option of its own', async () => {
    // 'o/only-queued' exists ONLY on a queued row, so filtering to it could
    // never match anything in the grouped list.
    const h = await mountReady([...LIVE, mk('q2', { status: 'queued', repo: 'o/only-queued' })])
    expect(h.result.current.filterOptions.project.map((o) => o.value)).toEqual(['o/a', 'o/b'])
  })
})

// ── R6: filters narrow the visible list, never the advance fallback ──────────

describe('advance fallback (R6)', () => {
  it('allOrderedSessions ignores the filters', async () => {
    const h = await mountReady()
    const unfiltered = ids(h.result.current.ordering.allOrderedSessions)
    expect(unfiltered.slice().sort()).toEqual(['askA', 'busyA', 'idleA', 'idleB', 'scratch'])

    act(() => h.result.current.setFilters(filters({ status: 'your-turn' })))
    expect(ids(h.result.current.ordering.allOrderedSessions)).toEqual(unfiltered)
    expect(sorted(h.result.current.ordering.orderedSessions))
      .toEqual(['idleA', 'idleB', 'scratch'])
  })

  it('archiving inside a filtered view advances to the next MATCHING row', async () => {
    const h = await mountReady()
    act(() => h.result.current.setFilters(filters({ project: 'o/a' })))
    const visible = ids(h.result.current.ordering.orderedSessions)
    const all = ids(h.result.current.ordering.allOrderedSessions)
    const next = nextSessionAfterClose(visible, all, visible[0])
    // The neighbour comes from the FILTERED list — filtered to o/a you continue
    // through o/a rather than jumping to an unrelated session.
    expect(next).toBe(visible[1])
    expect(['askA', 'busyA', 'idleA']).toContain(next)
  })

  it('falls back to the full ordering once the filtered set is exhausted', async () => {
    const h = await mountReady()
    act(() => h.result.current.setFilters(filters({ status: 'busy' })))
    const visible = ids(h.result.current.ordering.orderedSessions)
    expect(visible).toEqual(['busyA'])
    const all = ids(h.result.current.ordering.allOrderedSessions)
    const next = nextSessionAfterClose(visible, all, 'busyA')
    // Sole match: the visible list cannot answer, so the fallback does — and it
    // only can because `allOrderedSessions` still holds the filtered-out rows.
    expect(next).not.toBeNull()
    expect(next).not.toBe('busyA')
    expect(all).toContain(next)
  })
})

// ── the nav cursor stays valid under a shrinking list ────────────────────────

describe('nav cursor', () => {
  it('clamps into the FILTERED set — never onto a filtered-out session', async () => {
    const h = await mountReady()
    act(() => h.result.current.nav.navigate('down')) // 0
    act(() => h.result.current.nav.navigate('down')) // 1
    act(() => h.result.current.nav.navigate('down')) // 2
    act(() => h.result.current.nav.navigate('down')) // 3
    expect(h.result.current.nav.highlightedIndex).toBe(3)

    act(() => h.result.current.setFilters(filters({ status: 'busy' })))
    const visible = ids(h.result.current.ordering.orderedSessions)
    expect(visible).toEqual(['busyA'])
    expect(h.result.current.nav.highlightedIndex).toBe(0)
    // The load-bearing assertion: a stale index would leave `highlightedSession`
    // pointing at a row the list no longer shows, and Enter would open it.
    expect(h.result.current.nav.highlightedSession?.id).toBe('busyA')
    expect(visible).toContain(h.result.current.nav.highlightedSession?.id)
  })

  it('highlightSession finds the row again once a filter admits it', async () => {
    const h = await mountReady()
    act(() => h.result.current.setFilters(filters({ status: 'busy' })))
    act(() => h.result.current.nav.highlightSession('idleA'))
    expect(h.result.current.nav.highlightedSession?.id).not.toBe('idleA')

    act(() => h.result.current.setFilters(NO_FILTERS))
    act(() => h.result.current.nav.highlightSession('idleA'))
    expect(h.result.current.nav.highlightedSession?.id).toBe('idleA')
  })
})

// ── persistence ──────────────────────────────────────────────────────────────

describe('persistence', () => {
  it('setFilters writes into the existing sessionOrder object without dropping it', async () => {
    window.localStorage.setItem(ORDER_KEY, JSON.stringify({ groupBy: 'repo', sortBy: 'name' }))
    const h = await mountReady()
    act(() => h.result.current.setFilters(filters({ status: 'busy' })))
    const stored = JSON.parse(window.localStorage.getItem(ORDER_KEY) ?? '{}') as Record<string, unknown>
    expect(stored.filters).toEqual({ status: 'busy', project: null, environment: null })
    expect(stored.groupBy).toBe('repo')
    expect(stored.sortBy).toBe('name')
  })

  it('restores a stored selection on mount and applies it', async () => {
    window.localStorage.setItem(
      ORDER_KEY,
      JSON.stringify({ filters: { project: 'o/b', environment: 'rocky' } }),
    )
    const h = await mountReady()
    expect(h.result.current.filters).toEqual({ status: null, project: 'o/b', environment: 'rocky' })
    expect(ids(h.result.current.ordering.orderedSessions)).toEqual(['idleB'])
  })

  it('re-sanitizes when the options shrink and writes the correction back', async () => {
    window.localStorage.setItem(
      ORDER_KEY,
      JSON.stringify({ filters: { status: 'busy', project: 'o/gone', environment: 'retired' } }),
    )
    const h = await mountReady()
    // 'o/gone' matches no session and 'retired' is not in the registry: both must
    // fall back to "no filter", or the list is empty with no way out from the UI.
    expect(h.result.current.filters).toEqual({ status: 'busy', project: null, environment: null })
    await waitFor(() =>
      expect(storedFilters()).toEqual({ status: 'busy', project: null, environment: null }),
    )
  })
})

// ── the load-order hazard ────────────────────────────────────────────────────

describe('load order', () => {
  it('a stored filter survives a mount where the queries resolve on the second render', async () => {
    window.localStorage.setItem(
      ORDER_KEY,
      JSON.stringify({ filters: { project: 'o/b', environment: 'rocky' } }),
    )
    // Both queries unresolved: no sessions yet AND `/fleet/hosts` still in flight.
    let openGate = () => {}
    hostsGate = new Promise<void>((resolve) => { openGate = resolve })

    const h = mount({ sessions: [], sessionsLoading: true })

    // First render: every option list is empty. Sanitizing HERE is the bug —
    // it would drop both stored values and the user would find their filter
    // silently gone on every page load.
    expect(h.result.current.filterOptions.project).toEqual([])
    expect(h.result.current.filterOptions.environment).toEqual([])
    expect(h.result.current.filters).toEqual({ status: null, project: 'o/b', environment: 'rocky' })
    // …and nothing may be written back while the domains are unknown.
    expect(storedFilters()).toEqual({ project: 'o/b', environment: 'rocky' })

    // Second render: both queries answer.
    await act(async () => {
      openGate()
      await Promise.resolve()
    })
    h.rerender({ sessions: ALL, sessionsLoading: false })
    await waitFor(() =>
      expect(h.result.current.filterOptions.environment).toHaveLength(HOSTS.length),
    )

    expect(h.result.current.filters).toEqual({ status: null, project: 'o/b', environment: 'rocky' })
    expect(ids(h.result.current.ordering.orderedSessions)).toEqual(['idleB'])
    // Re-persisted in normalized form, with both values INTACT — the write-back
    // is what would have destroyed them had it run before the domains were known.
    expect(storedFilters()).toEqual({ status: null, project: 'o/b', environment: 'rocky' })
  })

  it('holds the stored value while ONLY the fleet query is outstanding', async () => {
    window.localStorage.setItem(ORDER_KEY, JSON.stringify({ filters: { environment: 'rocky' } }))
    let openGate = () => {}
    hostsGate = new Promise<void>((resolve) => { openGate = resolve })

    const h = mount({ sessions: ALL, sessionsLoading: false })
    // Sessions are in, the registry is not: the environment domain is still
    // unknown, so 'rocky' must not be sanitized away.
    expect(h.result.current.filters.environment).toBe('rocky')

    await act(async () => {
      openGate()
      await Promise.resolve()
    })
    await waitFor(() =>
      expect(h.result.current.filterOptions.environment).toHaveLength(HOSTS.length),
    )
    expect(h.result.current.filters.environment).toBe('rocky')
  })
})

// ── search composes with filters ─────────────────────────────────────────────

describe('search × filters', () => {
  it('the grouped list is the INTERSECTION of the filter and the search', async () => {
    const h = await mountReady()
    act(() => h.result.current.setFilters(filters({ project: 'o/a' })))
    act(() => h.result.current.search.setQuery('idle'))
    await waitFor(
      () => expect(ids(h.result.current.ordering.orderedSessions)).toEqual(['idleA']),
      { timeout: 3000 },
    )
    // idleB matches the search but not the filter; askA/busyA the reverse.
  })
})
