import { RefreshCw } from 'lucide-react'
import { cn } from '@/lib/utils'

interface NewVersionBannerProps {
  onReload: () => void
}

export function NewVersionBanner({ onReload }: NewVersionBannerProps) {
  return (
    <div
      role="status"
      className={cn(
        'fixed inset-x-0 top-0 z-50 flex items-center justify-center gap-3',
        'border-b border-sky-300 dark:border-sky-700 bg-sky-50 dark:bg-sky-900/90 px-4 py-2',
      )}
    >
      <RefreshCw size={16} className="text-sky-700 dark:text-sky-300 flex-shrink-0" aria-hidden="true" />
      <span className="text-sm font-medium text-sky-900 dark:text-sky-100">
        New version available
      </span>
      <button
        type="button"
        onClick={onReload}
        className={cn(
          'rounded-md bg-sky-600 px-3 py-1 text-xs font-semibold text-white',
          'hover:bg-sky-700 focus:outline-none focus:ring-2 focus:ring-sky-400',
        )}
      >
        Reload
      </button>
    </div>
  )
}
