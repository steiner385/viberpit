import { cn } from '@/lib/utils'
import type { GroupBy, SortBy } from '@/session/ordering'

interface SessionListControlsProps {
  groupBy: GroupBy
  sortBy: SortBy
  onGroupByChange: (g: GroupBy) => void
  onSortByChange: (s: SortBy) => void
}

const selectClass = cn(
  'text-xs rounded-md border border-gray-200 dark:border-gray-700',
  'bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300',
  'px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
)

export function SessionListControls({
  groupBy,
  sortBy,
  onGroupByChange,
  onSortByChange,
}: SessionListControlsProps) {
  return (
    <div className="flex items-center gap-3 flex-wrap">
      <label className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400">
        <span>Group by</span>
        <select
          aria-label="Group by"
          className={selectClass}
          value={groupBy}
          onChange={(e) => onGroupByChange(e.target.value as GroupBy)}
        >
          <option value="none">None</option>
          <option value="repo">Repo</option>
          <option value="status">Status</option>
        </select>
      </label>
      <label className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400">
        <span>Sort by</span>
        <select
          aria-label="Sort by"
          className={selectClass}
          value={sortBy}
          onChange={(e) => onSortByChange(e.target.value as SortBy)}
        >
          <option value="status">Status tier</option>
          <option value="lastActive">Last active</option>
          <option value="age">Age</option>
          <option value="name">Name</option>
        </select>
      </label>
    </div>
  )
}
