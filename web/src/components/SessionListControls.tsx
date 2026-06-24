import { X } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { GroupBy, SortBy } from '@/session/ordering'

export interface SelectOption<V extends string> {
  value: V
  label: string
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
}

const LIVE_GROUP_OPTIONS: SelectOption<GroupBy>[] = [
  { value: 'none', label: 'None' },
  { value: 'repo', label: 'Repo' },
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
)

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
      <label className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400">
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
      <label className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400">
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
