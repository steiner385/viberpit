import { RefreshCw } from 'lucide-react'
import { cn } from '@/lib/utils'

interface UpdatePendingBannerProps {
  queuedCount: number
  mode?: 'updating' | 'reconnecting'
}

export function UpdatePendingBanner({ queuedCount, mode = 'updating' }: UpdatePendingBannerProps) {
  const title = mode === 'updating' ? 'Update in progress' : 'Reconnecting…'
  const detail = mode === 'updating'
    ? 'Finishing the current turn, then applying a brief restart.'
    : 'Connection lost — your messages are held and will send once it reconnects.'
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
        <p className="text-sm font-medium text-sky-900 dark:text-sky-200">{title}</p>
        <p className="text-xs text-sky-700 dark:text-sky-400 mt-0.5">
          {detail}
          {queuedCount > 0 ? (
            <> {' '}{queuedCount} message{queuedCount === 1 ? '' : 's'} held.</>
          ) : null}
        </p>
      </div>
    </div>
  )
}
