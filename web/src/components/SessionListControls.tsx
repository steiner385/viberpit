import { X } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { GroupBy, SortBy } from '@/session/ordering'
import {
  NO_FILTERS, isFiltered,
  type FilterOption, type FilterOptions, type ListFilters,
} from '@/session/listFilters'

export interface SelectOption<V extends string> {
  value: V
  label: string
}

/**
 * The Status/Project/Environment selection, its domains, and the setter — one
 * prop, not three loose optionals, so "this list has filters" is a single
 * yes/no. ArchivedSessionList mounts the same controls without it and keeps exactly
 * the search/group/sort strip it has today.
 *
 * `options` is the UNFILTERED domain (see `useSessionListState`); the value and
 * option shape are `listFilters`', not restated here — `FilterOption` is
 * structurally `SelectOption<string>` and is consumed directly.
 */
export interface FilterControls {
  value: ListFilters
  options: FilterOptions
  onChange: (next: ListFilters) => void
}

interface SessionListControlsProps<G extends string, S extends string> {
  groupBy: G
  sortBy: S
  onGroupByChange: (g: G) => void
  onSortByChange: (s: S) => void
  searchQuery: string
  onSearchChange: (q: string) => void
  groupOptions?: SelectOption<G>[]
  sortOptions?: SelectOption<S>[]
  searchPlaceholder?: string
  searchAriaLabel?: string
  /** Omit to render no filter selects at all (ArchivedSessionList). */
  filters?: FilterControls
}

// DEC-7: the `repo` option READS "Project" and its VALUE is still `repo`.
// `LIVE_SPEC.groupByValues` is persisted under `ccswitch.sessionOrder`, so
// introducing a `project` value here would strand every saved preference on a
// value the parser rejects. This is a relabel and only a relabel — do not
// "finish" it by renaming the value.
const LIVE_GROUP_OPTIONS: SelectOption<GroupBy>[] = [
  { value: 'none', label: 'None' },
  { value: 'repo', label: 'Project' },
  { value: 'status', label: 'Status' },
]
const LIVE_SORT_OPTIONS: SelectOption<SortBy>[] = [
  { value: 'status', label: 'Status tier' },
  { value: 'lastActive', label: 'Last active' },
  { value: 'age', label: 'Age' },
  { value: 'name', label: 'Name' },
]

const selectClass = cn(
  'text-xs rounded-md border border-gray-200 dark:border-gray-700',
  'bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300',
  'px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
  'disabled:opacity-60',
)
const labelClass = 'flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400'

/** `<option>` value standing for "no filter". Safe as a sentinel because
 *  `coerceFilters` already treats the empty string as null and
 *  `deriveFilterOptions` never emits a blank value. */
const ALL = ''

interface FilterSelectProps {
  /** Visible text and accessible name — one string, so they cannot drift. */
  label: string
  /** First option's text ("All projects"). */
  allLabel: string
  value: string | null
  options: readonly FilterOption[]
  onChange: (next: string | null) => void
}

/**
 * One filter select.
 *
 * Two states that look alike but are not:
 *
 *  · **no options and no selection** — the domain is empty (no fleet host is
 *    registered, no session has a repo). The control is rendered anyway, so the
 *    strip does not reflow as sessions come and go, but it is `disabled`:
 *    nothing is selectable.
 *  · **a selection the options do not (yet) offer** — the fleet query has not
 *    resolved while a STORED environment filter is already narrowing the list
 *    (`useSessionListState` deliberately declines to sanitize before the domains
 *    are known). Built naively from `options`, the select would fall back to
 *    "All environments" and claim no filter while the list is filtered. So the
 *    raw value is rendered as an option and the control stays enabled — the
 *    filter shows as applied, and it can be cleared.
 */
function FilterSelect({ label, allLabel, value, options, onChange }: FilterSelectProps) {
  const offered = value !== null && !options.some((o) => o.value === value)
    ? [...options, { value, label: value }]
    : options
  return (
    <label className={labelClass}>
      <span>{label}</span>
      <select
        aria-label={label}
        className={selectClass}
        value={value ?? ALL}
        disabled={offered.length === 0}
        onChange={(e) => onChange(e.target.value === ALL ? null : e.target.value)}
      >
        <option value={ALL}>{allLabel}</option>
        {offered.map((o) => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
    </label>
  )
}

export function SessionListControls<G extends string = GroupBy, S extends string = SortBy>({
  groupBy,
  sortBy,
  onGroupByChange,
  onSortByChange,
  searchQuery,
  onSearchChange,
  groupOptions = LIVE_GROUP_OPTIONS as unknown as SelectOption<G>[],
  sortOptions = LIVE_SORT_OPTIONS as unknown as SelectOption<S>[],
  searchPlaceholder = 'Search sessions…',
  searchAriaLabel = 'Search sessions',
  filters,
}: SessionListControlsProps<G, S>) {
  return (
    <div className="flex items-center gap-3 flex-wrap">
      <div className="relative">
        <input
          type="search"
          aria-label={searchAriaLabel}
          placeholder={searchPlaceholder}
          value={searchQuery}
          onChange={(e) => onSearchChange(e.target.value)}
          className={cn(selectClass, 'pr-7 w-44 [&::-webkit-search-cancel-button]:hidden')}
        />
        {searchQuery !== '' && (
          <button
            type="button"
            aria-label="Clear search"
            onClick={() => onSearchChange('')}
            className="absolute right-1 top-1/2 -translate-y-1/2 p-0.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
          >
            <X size={14} aria-hidden="true" />
          </button>
        )}
      </div>
      {filters && (
        <>
          <FilterSelect
            label="Status"
            allLabel="All statuses"
            value={filters.value.status}
            options={filters.options.status}
            onChange={(status) => filters.onChange({ ...filters.value, status })}
          />
          <FilterSelect
            label="Project"
            allLabel="All projects"
            value={filters.value.project}
            options={filters.options.project}
            onChange={(project) => filters.onChange({ ...filters.value, project })}
          />
          <FilterSelect
            label="Environment"
            allLabel="All environments"
            value={filters.value.environment}
            options={filters.options.environment}
            onChange={(environment) => filters.onChange({ ...filters.value, environment })}
          />
          {/* Routed through `listFilters.isFiltered` rather than a second
              hand-rolled "is anything set" — and it lives HERE, in the strip,
              because the strip is gated on the session count and survives a
              filter that matches nothing. The list's own no-matches button
              disappears the moment the filter is cleared; this one is the
              escape hatch that is always where the user set the filter. */}
          {isFiltered(filters.value) && (
            <button
              type="button"
              onClick={() => filters.onChange(NO_FILTERS)}
              className={cn(
                'text-xs rounded-md px-2 py-1 font-medium',
                'text-blue-600 dark:text-blue-400 hover:underline',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
              )}
            >
              Clear filters
            </button>
          )}
        </>
      )}
      <label className={labelClass}>
        <span>Group by</span>
        <select
          aria-label="Group by"
          className={selectClass}
          value={groupBy}
          onChange={(e) => onGroupByChange(e.target.value as G)}
        >
          {groupOptions.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      </label>
      <label className={labelClass}>
        <span>Sort by</span>
        <select
          aria-label="Sort by"
          className={selectClass}
          value={sortBy}
          onChange={(e) => onSortByChange(e.target.value as S)}
        >
          {sortOptions.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      </label>
    </div>
  )
}
