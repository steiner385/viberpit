import { RefreshCw } from 'lucide-react'
import { cn } from '@/lib/utils'

interface UpdatePendingBannerProps {
  queuedCount: number
}

export function UpdatePendingBanner({ queuedCount }: UpdatePendingBannerProps) {
  return (
    <div
      role="status"
      className={cn(
        'flex items-start gap-3 rounded-lg border border-sky-300 dark:border-sky-700',
        'bg-sky-50 dark:bg-sky-900/20 px-4 py-3',
      )}
    >
      <RefreshCw size={18} className="text-sky-700 dark:text-sky-400 flex-shrink-0 mt-0.5 animate-spin" aria-hidden="true" />
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium text-sky-900 dark:text-sky-200">
          Update in progress
        </p>
        <p className="text-xs text-sky-700 dark:text-sky-400 mt-0.5">
          Finishing the current turn, then applying a brief restart.
          {queuedCount > 0 ? (
            <> {' '}{queuedCount} message{queuedCount === 1 ? '' : 's'} will send after it completes.</>
          ) : null}
        </p>
      </div>
    </div>
  )
}
