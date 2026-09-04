import { describe, it, expect, beforeEach } from 'vitest'
import type { Session } from '@/api/types'
import {
  NO_FILTERS, coerceFilters, deriveFilterOptions, filterSessions, isFiltered, sanitizeFilters,
  type ListFilters,
} from '../listFilters'
import { loadListFilters, loadOrderPref, saveListFilters, saveOrderPref } from '@/lib/sessionOrder'

// Typed factory with defaults — real Session values, never `as unknown as`, so
// a fixture that drifts from the real shape (a status string that does not
// exist, a field belonging to another type) is a type error here.
function mk(id: string, over: Partial<Session> = {}): Session {
  return {
    id, name: id, accountName: 'acct', status: 'idle',
    createdAt: '2026-06-01T00:00:00Z', prs: [], ...over,
  }
}

const filters = (over: Partial<ListFilters> = {}): ListFilters => ({ ...NO_FILTERS, ...over })

// status 'idle' → tier 'your-turn'; 'waiting_input' → 'needs-you'; 'in_progress' → 'busy'.
const idleA = mk('idleA', { repo: 'o/a', host: 'dobby' })
const idleB = mk('idleB', { repo: 'o/b', host: 'rocky' })
const busyA = mk('busyA', { status: 'in_progress', repo: 'o/a', host: 'rocky' })
const askA = mk('askA', { status: 'waiting_input', repo: 'o/a', host: 'dobby' })
const scratch = mk('scratch')
const ALL = [idleA, idleB, busyA, askA, scratch]
const ids = (list: readonly Session[]): string[] => list.map((s) => s.id)

const OPTIONS = deriveFilterOptions(ALL, ['dobby', 'rocky', 'edge'])

describe('isFiltered', () => {
  it('is false only when every filter is null', () => {
    expect(isFiltered(NO_FILTERS)).toBe(false)
    expect(isFiltered(filters({ status: 'busy' }))).toBe(true)
    expect(isFiltered(filters({ project: 'o/a' }))).toBe(true)
    expect(isFiltered(filters({ environment: 'dobby' }))).toBe(true)
  })
})

describe('filterSessions', () => {
  it('null means "all" — no filter set keeps every session, in order', () => {
    expect(ids(filterSessions(ALL, NO_FILTERS))).toEqual(ids(ALL))
  })

  it('status alone matches the triage TIER, not the raw status string', () => {
    // 'your-turn' is a tier no Session.status is ever equal to, so a predicate
    // comparing s.status would return nothing here.
    expect(ids(filterSessions(ALL, filters({ status: 'your-turn' })))).toEqual(
      ['idleA', 'idleB', 'scratch'],
    )
    expect(ids(filterSessions(ALL, filters({ status: 'busy' })))).toEqual(['busyA'])
    expect(ids(filterSessions(ALL, filters({ status: 'needs-you' })))).toEqual(['askA'])
  })

  it('project alone keeps that repo and excludes repo-less (scratch) sessions', () => {
    expect(ids(filterSessions(ALL, filters({ project: 'o/a' })))).toEqual(
      ['idleA', 'busyA', 'askA'],
    )
    expect(ids(filterSessions(ALL, filters({ project: 'o/b' })))).toEqual(['idleB'])
  })

  it('environment alone keeps that host and excludes host-less sessions', () => {
    expect(ids(filterSessions(ALL, filters({ environment: 'dobby' })))).toEqual(['idleA', 'askA'])
    expect(ids(filterSessions(ALL, filters({ environment: 'rocky' })))).toEqual(['idleB', 'busyA'])
  })

  it('a null/absent repo or host never matches an active filter', () => {
    const nulls = [mk('n1', { repo: null, host: null }), mk('n2')]
    expect(filterSessions(nulls, filters({ project: 'o/a' }))).toEqual([])
    expect(filterSessions(nulls, filters({ environment: 'dobby' }))).toEqual([])
    // …but they are still matched by a status filter, which every session has.
    expect(ids(filterSessions(nulls, filters({ status: 'your-turn' })))).toEqual(['n1', 'n2'])
  })

  it('combines the three with AND — matching two of three is not enough', () => {
    const all3 = filters({ status: 'busy', project: 'o/a', environment: 'rocky' })
    expect(ids(filterSessions(ALL, all3))).toEqual(['busyA'])
    // busyA is on o/a + rocky but is 'busy', so a status of 'your-turn' excludes
    // it; under OR it would still be returned by the project/environment legs.
    expect(filterSessions(ALL, { ...all3, status: 'your-turn' })).toEqual([])
    expect(filterSessions(ALL, { ...all3, project: 'o/b' })).toEqual([])
    expect(filterSessions(ALL, { ...all3, environment: 'dobby' })).toEqual([])
  })

  it('routes the repo through formatRepoLabel, so a padded repo still matches', () => {
    const padded = mk('padded', { repo: '  o/a  ' })
    expect(ids(filterSessions([padded], filters({ project: 'o/a' })))).toEqual(['padded'])
    // A blank repo is scratch, not a project named ''.
    expect(filterSessions([mk('blank', { repo: '   ' })], filters({ project: '' }))).toEqual([])
  })
})

describe('deriveFilterOptions', () => {
  it('offers every status tier, ordered by triage rank, regardless of the data', () => {
    expect(deriveFilterOptions([], []).status.map((o) => o.value)).toEqual(
      ['needs-you', 'unreachable', 'error', 'your-turn', 'waiting', 'busy'],
    )
    expect(OPTIONS.status.map((o) => o.label)).toContain('Needs attention')
  })

  it('derives projects from the sessions, deduped, sorted, scratch excluded', () => {
    expect(OPTIONS.project.map((o) => o.value)).toEqual(['o/a', 'o/b'])
  })

  it('derives environments from the fleet registry, not from the sessions', () => {
    // 'edge' runs no session but is a configured host — a legitimate choice.
    expect(OPTIONS.environment.map((o) => o.value)).toEqual(['dobby', 'edge', 'rocky'])
  })
})

describe('sanitizeFilters', () => {
  it('keeps values the options offer', () => {
    const f = filters({ status: 'busy', project: 'o/a', environment: 'edge' })
    expect(sanitizeFilters(f, OPTIONS)).toEqual(f)
  })

  it('drops values the options no longer offer, one leg at a time', () => {
    expect(sanitizeFilters(filters({ status: 'exploded' }), OPTIONS).status).toBeNull()
    expect(sanitizeFilters(filters({ project: 'o/gone' }), OPTIONS).project).toBeNull()
    expect(sanitizeFilters(filters({ environment: 'retired' }), OPTIONS).environment).toBeNull()
  })

  it('drops only the stale leg, keeping the valid ones', () => {
    const f = filters({ status: 'busy', project: 'o/gone', environment: 'dobby' })
    expect(sanitizeFilters(f, OPTIONS)).toEqual(filters({ status: 'busy', environment: 'dobby' }))
  })
})

describe('coerceFilters', () => {
  it('turns anything that is not a non-empty string into null', () => {
    expect(coerceFilters(undefined)).toEqual(NO_FILTERS)
    expect(coerceFilters(null)).toEqual(NO_FILTERS)
    expect(coerceFilters('busy')).toEqual(NO_FILTERS)
    expect(coerceFilters([1, 2])).toEqual(NO_FILTERS)
    expect(coerceFilters({ status: 7, project: '', environment: '  ' })).toEqual(NO_FILTERS)
  })

  it('trims the values it keeps', () => {
    expect(coerceFilters({ status: ' busy ' })).toEqual(filters({ status: 'busy' }))
  })
})

describe('persistence — the same namespaced ordering pref, not a new key', () => {
  beforeEach(() => window.localStorage.clear())

  it('stores filters inside ccswitch.sessionOrder and adds no second key', () => {
    saveListFilters('live', filters({ status: 'busy' }))
    const raw = window.localStorage.getItem('ccswitch.sessionOrder')
    expect(raw).not.toBeNull()
    expect(JSON.parse(raw as string)).toEqual({ filters: filters({ status: 'busy' }) })
    expect(window.localStorage.getItem('ccswitch.listFilters')).toBeNull()
    expect(Object.keys(window.localStorage)).toEqual(['ccswitch.sessionOrder'])
  })

  it('namespaces non-live lists exactly like the ordering half does', () => {
    saveListFilters('archived', filters({ project: 'o/a' }))
    expect(window.localStorage.getItem('ccswitch.sessionOrder')).toBeNull()
    expect(window.localStorage.getItem('ccswitch.sessionOrder.archived')).not.toBeNull()
  })

  it('saving filters preserves the ordering half, and vice versa', () => {
    saveOrderPref('live', { groupBy: 'repo', sortBy: 'name' })
    saveListFilters('live', filters({ project: 'o/b' }))
    expect(loadOrderPref('live', ['none', 'repo'], ['status', 'name'], {
      groupBy: 'none', sortBy: 'status',
    })).toEqual({ groupBy: 'repo', sortBy: 'name' })
    expect(loadListFilters('live', OPTIONS)).toEqual(filters({ project: 'o/b' }))

    saveOrderPref('live', { groupBy: 'none', sortBy: 'status' })
    expect(loadListFilters('live', OPTIONS)).toEqual(filters({ project: 'o/b' }))
  })

  it('returns NO_FILTERS when nothing is stored or the entry is corrupt', () => {
    expect(loadListFilters('live', OPTIONS)).toEqual(NO_FILTERS)
    window.localStorage.setItem('ccswitch.sessionOrder', '{not json')
    expect(loadListFilters('live', OPTIONS)).toEqual(NO_FILTERS)
  })

  // Omitting `options` is the load-order escape hatch: the option domains come
  // from two React Query results, and on the first render both are empty, so
  // sanitizing there would discard every stored project/environment filter on
  // every page load. The caller holds this raw value until its queries resolve.
  it('without options it coerces but does NOT validate against a domain', () => {
    window.localStorage.setItem(
      'ccswitch.sessionOrder',
      JSON.stringify({ filters: { status: 'busy', project: '  o/a  ', environment: 42 } }),
    )
    // Same store, empty domains: with options every value is dropped…
    expect(loadListFilters('live', deriveFilterOptions([], []))).toEqual(
      filters({ status: 'busy' }),
    )
    // …without them the open-domain values survive, and coercion still applies
    // (trimmed string, non-string → null).
    expect(loadListFilters('live')).toEqual(filters({ status: 'busy', project: 'o/a' }))
  })
})

describe('a stored value that no longer exists falls back — it must not empty the list', () => {
  beforeEach(() => window.localStorage.clear())

  // Each leg: the stored filter is dead, so the control that would clear it
  // cannot render it — if it survived, the user would face an empty list with
  // no way out of it.
  const store = (f: Partial<ListFilters>): void => {
    window.localStorage.setItem('ccswitch.sessionOrder', JSON.stringify({ filters: filters(f) }))
  }

  it('a retired fleet host falls back, and the whole list comes back', () => {
    store({ environment: 'retired-host' })
    const loaded = loadListFilters('live', OPTIONS)
    expect(loaded.environment).toBeNull()
    expect(ids(filterSessions(ALL, loaded))).toEqual(ids(ALL))
  })

  it('a repo no session uses any more falls back, and the whole list comes back', () => {
    store({ project: 'o/deleted' })
    const loaded = loadListFilters('live', OPTIONS)
    expect(loaded.project).toBeNull()
    expect(ids(filterSessions(ALL, loaded))).toEqual(ids(ALL))
  })

  it('a status tier that is not in the vocabulary falls back', () => {
    store({ status: 'exploded' })
    const loaded = loadListFilters('live', OPTIONS)
    expect(loaded.status).toBeNull()
    expect(ids(filterSessions(ALL, loaded))).toEqual(ids(ALL))
  })

  // The counter-cases: "matches nothing" is NOT the same as "no longer exists".
  // A real option with zero current members must survive and show an empty list,
  // or a filter would silently clear itself the moment the last match resolved.
  it('a real tier with zero sessions survives and yields an empty list', () => {
    store({ status: 'error' })
    const loaded = loadListFilters('live', OPTIONS)
    expect(loaded.status).toBe('error')
    expect(filterSessions(ALL, loaded)).toEqual([])
  })

  it('a registered host with zero sessions survives and yields an empty list', () => {
    store({ environment: 'edge' })
    const loaded = loadListFilters('live', OPTIONS)
    expect(loaded.environment).toBe('edge')
    expect(filterSessions(ALL, loaded)).toEqual([])
  })
})
