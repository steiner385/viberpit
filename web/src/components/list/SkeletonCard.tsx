//
// Moved verbatim from SessionList.tsx. Kept as its own module so the archived
// list can adopt the same loading shape rather than growing a second one.
import { cn } from '@/lib/utils'

export function SkeletonCard() {
  return (
    <div
      className={cn(
        'rounded-lg border border-gray-200 dark:border-gray-700',
        'bg-white dark:bg-gray-800 p-3 min-h-[44px] animate-pulse',
      )}
      aria-hidden="true"
    >
      <div className="flex items-center gap-3">
        <div className="flex-1 space-y-2">
          <div className="h-3 bg-gray-200 dark:bg-gray-700 rounded w-1/3" />
          <div className="h-2 bg-gray-200 dark:bg-gray-700 rounded w-1/2" />
        </div>
        <div className="h-8 w-8 bg-gray-200 dark:bg-gray-700 rounded" />
      </div>
    </div>
  )
}
