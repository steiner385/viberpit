// web/src/features/sessions/useSessionListState.ts
//
// The Sessions list's state, in one place.
//
// Dashboard and DesktopShell each open-coded this wiring inline, and they drifted:
// the partition that decides which rows are held OUT of the grouped list was
// `!isPreSpawnStatus(s.status)` on phone but `s.status !== 'queued'` on desktop, so
// `pending_approval` rows leaked into the grouped list on wide screens — where no
// PendingApprovalSection existed to show them properly either. DEC-3 settles it on
// `isPreSpawnStatus`, and this hook is the single place that applies it.
//
// It is a hook, not a component-private `useMemo`, because the shell above the
// column needs the same handles the column renders from: the prompt bar drives
// `nav` (↑/↓, jump, expand/collapse) and close/archive advance uses
// `ordering.orderedSessions`. One call site, one state — not two copies that can
// disagree about which session is highlighted.
import { useEffect, useMemo, useState } from 'react'
import { isPreSpawnStatus, type Session } from '@/api/types'
import { useFleetHosts } from '@/api/fleet'
import { useSessionSearch, type SessionSearch } from '@/session/useSessionSearch'
import { useSessionOrdering, LIVE_SPEC, type SessionOrdering } from '@/session/useSessionOrdering'
import { useListNavigation, type ListNavigation } from '@/session/useListNavigation'
import {
  deriveFilterOptions, filterSessions, sanitizeFilters,
  type FilterOptions, type ListFilters,
} from '@/session/listFilters'
import { loadListFilters, saveListFilters } from '@/lib/sessionOrder'

export interface SessionListState {
  /** Every live session, pre-spawn rows included — what the list-controls gate counts. */
  sessions: readonly Session[]
  /** Waiting for a capacity slot, in queue order (QueuedSection). */
  queued: Session[]
  /** Parked at the launch-time model gate (PendingApprovalSection). */
  pendingApproval: Session[]
  /** Search box state; `matchedIds` already feeds `ordering`. */
  search: SessionSearch
  /** Status/Project/Environment selection (DEC-8), already applied to `ordering`. */
  filters: ListFilters
  /** Replace the selection. It is persisted, and a value the options do not
   *  offer is dropped on the next render rather than silently kept. */
  setFilters: (next: ListFilters) => void
  /** What the filter controls may offer — derived from the UNFILTERED list and
   *  the fleet registry, so choosing a value never empties its own dropdown. */
  filterOptions: FilterOptions
  /** Group/sort/collapse state and the derived groups + navItems. */
  ordering: SessionOrdering
  /** Keyboard cursor over `ordering.navItems`. */
  nav: ListNavigation
}

export interface SessionListStateOptions {
  /** True while the sessions query has not resolved. Needed because an empty
   *  `sessions` array means two different things — "no sessions" and "not
   *  loaded yet" — and only the first may sanitize a stored project filter
   *  away. See `ready` below. */
  sessionsLoading?: boolean
}

const NS = LIVE_SPEC.namespace

/** Field-wise equality — `sanitizeFilters` always returns a fresh object, so
 *  identity would report a change on every render and loop the state update. */
function sameFilters(a: ListFilters, b: ListFilters): boolean {
  return a.status === b.status && a.project === b.project && a.environment === b.environment
}

/**
 * Partition the live sessions (DEC-3) and derive search → ordering → navigation
 * from the non-pre-spawn remainder.
 *
 * Pre-spawn sessions are rendered by their own sections and must not enter
 * `useSessionOrdering`: when groups are provided, SessionList renders the groups
 * and ignores its `sessions` prop, so filtering after the fact leaves those rows
 * in the grouped list (their status has no tier mapping and falls through to the
 * your-turn bucket).
 *
 * The Status/Project/Environment filters (DEC-8) are applied in that same slot
 * and for that same reason: filter → search → group, never group → filter. They
 * narrow the GROUPED list only. `queued` and `pendingApproval` stay whole,
 * because a pre-spawn row has no tier (see above) and no host yet, so two of the
 * three filters could not narrow them at all; whether those sections should hide
 * while a filter is active is a rendering question for the controls, not a
 * second filtering rule here.
 */
export function useSessionListState(
  sessions: readonly Session[],
  { sessionsLoading = false }: SessionListStateOptions = {},
): SessionListState {
  const queued = useMemo(
    () =>
      sessions
        .filter((s) => s.status === 'queued')
        .sort((a, b) => (a.queuePosition ?? 0) - (b.queuePosition ?? 0)),
    [sessions],
  )
  const pendingApproval = useMemo(
    () => sessions.filter((s) => s.status === 'pending_approval'),
    [sessions],
  )
  const listed = useMemo(() => sessions.filter((s) => !isPreSpawnStatus(s.status)), [sessions])

  // `useFleetHosts`, NOT `useEnabledFleetHosts`: a session can still be running
  // on a host an operator has since disabled, and the environment filter has to
  // be able to select it. The registry (not the hosts seen on sessions) is the
  // domain, so a configured-but-idle host stays selectable and yields an empty
  // list rather than disappearing from the control.
  const fleet = useFleetHosts()
  const hostNames = useMemo(
    () => (fleet.data?.hosts ?? []).map((h) => h.name),
    [fleet.data],
  )
  // Derived from `listed` — the set the filters actually narrow, BEFORE they
  // narrow it. Deriving from the filtered rows instead would empty each
  // dropdown the moment it was used.
  const filterOptions = useMemo(
    () => deriveFilterOptions(listed, hostNames),
    [listed, hostNames],
  )

  // Loaded RAW — `loadListFilters` is deliberately called WITHOUT options.
  // Sanitizing against `filterOptions` before both queries resolve would discard
  // every project/environment filter on every page load: on the first render
  // `listed` is empty and the fleet query has no data, so no stored value is
  // offered by anything, and the user could not tell the loss from "never saved".
  const [filters, setFilters] = useState<ListFilters>(() => loadListFilters(NS))
  const ready = !sessionsLoading && !fleet.isPending
  // Adjust-state-during-render (React's documented pattern), NOT an effect: once
  // the domains are known, a value they no longer offer — a retired fleet host,
  // a repo no session uses — is dropped. React re-runs this body with the
  // corrected state before committing, so `filters` below is already the
  // sanitized value and keeps a stable identity between renders.
  const sane = ready ? sanitizeFilters(filters, filterOptions) : filters
  if (!sameFilters(sane, filters)) setFilters(sane)

  // localStorage is an external system, so the write is an effect. Persisting
  // the SANITIZED value is the point of doing it here rather than only in
  // `setFilters`: a dead value must not linger in storage and come back on the
  // next load. Gated on `ready` for the same reason the sanitize is.
  useEffect(() => {
    if (!ready) return
    saveListFilters(NS, filters)
  }, [ready, filters])

  const visible = useMemo(() => filterSessions(listed, filters), [listed, filters])

  const search = useSessionSearch()
  // `listed` as the third argument, not `visible`: filters narrow the visible
  // list only, never `allOrderedSessions` — the close/archive fallback. So
  // archiving inside a filtered view advances to the next row THAT MATCHES the
  // filter, and only falls back to the full ordering once the filtered set is
  // exhausted, instead of stranding the user on an empty pane.
  const ordering = useSessionOrdering(visible, search.matchedIds, listed)
  const nav = useListNavigation(ordering.navItems)

  return {
    sessions, queued, pendingApproval, search,
    filters, setFilters, filterOptions, ordering, nav,
  }
}
