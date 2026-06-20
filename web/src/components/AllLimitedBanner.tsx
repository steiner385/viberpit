import { Ban, X } from 'lucide-react'
import { cn } from '@/lib/utils'

interface AllLimitedBannerProps {
  earliestReset: string | null  // ISO timestamp
  onDismiss: () => void
}

export function AllLimitedBanner({ earliestReset, onDismiss }: AllLimitedBannerProps) {
  const resetLabel = earliestReset
    ? new Intl.DateTimeFormat(undefined, {
        hour: 'numeric',
        minute: '2-digit',
        weekday: 'short',
        month: 'short',
        day: 'numeric',
      }).format(new Date(earliestReset))
    : null

  return (
    <div
      role="alert"
      className={cn(
        'flex items-start gap-3 rounded-lg border border-amber-300 dark:border-amber-700',
        'bg-amber-50 dark:bg-amber-900/20 px-4 py-3',
      )}
    >
      <Ban
        size={18}
        className="text-amber-700 dark:text-amber-400 flex-shrink-0 mt-0.5"
        aria-hidden="true"
      />
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium text-amber-900 dark:text-amber-200">
          All accounts at limit
        </p>
        {resetLabel ? (
          <p className="text-xs text-amber-700 dark:text-amber-400 mt-0.5">
            Capacity returns at{' '}
            <time dateTime={earliestReset ?? undefined}>{resetLabel}</time>
          </p>
        ) : (
          <p className="text-xs text-amber-700 dark:text-amber-400 mt-0.5">
            No reset time available
          </p>
        )}
      </div>
      <button
        type="button"
        className={cn(
          'flex-shrink-0 h-8 w-8 flex items-center justify-center rounded',
          'text-amber-700 dark:text-amber-400',
          'hover:bg-amber-100 dark:hover:bg-amber-800/40',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500',
        )}
        onClick={onDismiss}
        aria-label="Dismiss alert"
      >
        <X size={14} aria-hidden="true" />
      </button>
    </div>
  )
}
