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
//
// Since the Background scope there are TWO partitions, applied in this order:
//
//   1. attention (`partitionByAttention`): quiet maintenance sessions leave for
//      the Background scope; everything else — human sessions, development-class
//      automation sessions, and any maintenance session that currently needs a
//      human — is the Active scope's input.
//   2. pre-spawn (DEC-3), on whichever side a scope is rendering.
//
// The pipeline below the split (filters → search → ordering → nav) is ONE
// function, `useListPipeline`, run once per scope under its own persistence
// namespace. `useSessionListState` is the Active scope's call (the shell's
// handle); `useBackgroundListState` is the Background scope's, mounted inside
// its body so its state lives only while it is on screen.
import { useEffect, useMemo, useState } from 'react'
import { isPreSpawnStatus, type Session } from '@/api/types'
import { useFleetHosts } from '@/api/fleet'
import { useSessionSearch, type SessionSearch } from '@/session/useSessionSearch'
import {
  useItemOrdering, LIVE_SPEC, BACKGROUND_SPEC,
  type OrderingSpec, type SessionOrdering,
} from '@/session/useSessionOrdering'
import type { GroupBy, SortBy } from '@/session/ordering'
import { useListNavigation, type ListNavigation } from '@/session/useListNavigation'
import {
  deriveFilterOptions, filterSessions, sanitizeFilters,
  type FilterOptions, type ListFilters,
} from '@/session/listFilters'
import { partitionByAttention } from '@/session/backgroundSessions'
import { loadListFilters, saveListFilters } from '@/lib/sessionOrder'

/** One scope's list state: the pipeline from its input rows to rendered order. */
export interface ListPipelineState {
  /** Every session in THIS scope, pre-spawn rows included — what the
   *  list-controls gate and the scope segment's count read. */
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

/** The Active scope's state — the shell's handle — plus the rows it handed to
 *  the Background scope, so the column can count them on the segment. */
export interface SessionListState extends ListPipelineState {
  /** Quiet maintenance sessions, held out of `sessions` and every derived
   *  field. `useBackgroundListState` runs the same pipeline over them. */
  background: readonly Session[]
}

export interface SessionListStateOptions {
  /** True while the sessions query has not resolved. Needed because an empty
   *  `sessions` array means two different things — "no sessions" and "not
   *  loaded yet" — and only the first may sanitize a stored project filter
   *  away. See `ready` below. */
  sessionsLoading?: boolean
}

/** Field-wise equality — `sanitizeFilters` always returns a fresh object, so
 *  identity would report a change on every render and loop the state update. */
function sameFilters(a: ListFilters, b: ListFilters): boolean {
  return a.status === b.status && a.project === b.project && a.environment === b.environment
}

/**
 * Partition one scope's sessions (DEC-3) and derive search → ordering →
 * navigation from the non-pre-spawn remainder.
 *
 * Pre-spawn sessions are rendered by their own sections and must not enter
 * `useItemOrdering`: when groups are provided, SessionList renders the groups
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
 *
 * `spec.namespace` keys the persisted filters, group-by, sort and collapse, so
 * each scope remembers its own.
 */
function useListPipeline(
  sessions: readonly Session[],
  spec: OrderingSpec<GroupBy, SortBy, Session>,
  { sessionsLoading = false }: SessionListStateOptions,
): ListPipelineState {
  const ns = spec.namespace
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
  const [filters, setFilters] = useState<ListFilters>(() => loadListFilters(ns))
  // `fleet.isSuccess`, NOT `!fleet.isPending`: in TanStack Query v5 `isPending`
  // is `status === 'pending'`, so a FAILED `/fleet/hosts` (status 'error') makes
  // it false and would declare the environment domain "known" while `fleet.data`
  // is undefined. Sanitizing against that empty domain drops the stored host and
  // the effect below writes the clearing back to storage, so it does not return
  // when the endpoint recovers. An unanswered registry and a failed one are the
  // same fact — the domain is UNKNOWN — and only success says otherwise.
  const ready = !sessionsLoading && fleet.isSuccess
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
    saveListFilters(ns, filters)
  }, [ready, ns, filters])

  const visible = useMemo(() => filterSessions(listed, filters), [listed, filters])

  const search = useSessionSearch()
  // `listed` as the fourth argument, not `visible`: filters narrow the visible
  // list only, never `allOrderedSessions` — the close/archive fallback. So
  // archiving inside a filtered view advances to the next row THAT MATCHES the
  // filter, and only falls back to the full ordering once the filtered set is
  // exhausted, instead of stranding the user on an empty pane.
  const ordering = useItemOrdering(visible, spec, search.matchedIds, listed)
  const nav = useListNavigation(ordering.navItems)

  return {
    sessions, queued, pendingApproval, search,
    filters, setFilters, filterOptions, ordering, nav,
  }
}

/**
 * The Active scope: every live session minus the quiet maintenance ones, run
 * through the pipeline under the 'live' namespace. Called ONCE, in the shell.
 */
export function useSessionListState(
  sessions: readonly Session[],
  options: SessionListStateOptions = {},
): SessionListState {
  const { foreground, background } = useMemo(() => partitionByAttention(sessions), [sessions])
  const pipeline = useListPipeline(foreground, LIVE_SPEC, options)
  return useMemo(() => ({ ...pipeline, background }), [pipeline, background])
}

/**
 * The Background scope: the rows `useSessionListState` held out, through the
 * same pipeline under their own namespace. Mounted inside `BackgroundScopeBody`
 * only, so its search and ordering state exist only while that scope is open.
 */
export function useBackgroundListState(
  background: readonly Session[],
  options: SessionListStateOptions = {},
): ListPipelineState {
  return useListPipeline(background, BACKGROUND_SPEC, options)
}
