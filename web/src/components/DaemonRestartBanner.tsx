import { Loader2 } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useDaemonHealth } from '@/hooks/useDaemonHealth'

/**
 * Sticky top banner shown while the session daemon is unreachable (e.g. during a
 * deploy restart). Auto-clears once /healthz reports daemon: "ok" again — no manual
 * dismiss needed since it resolves on its own.
 */
export function DaemonRestartBanner() {
  const daemonOk = useDaemonHealth()
  if (daemonOk) return null
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        'fixed inset-x-0 top-0 z-50 flex items-center justify-center gap-3',
        'border-b border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-900/90 px-4 py-2',
      )}
    >
      <Loader2 size={16} className="text-amber-700 dark:text-amber-300 flex-shrink-0 animate-spin" aria-hidden="true" />
      <span className="text-sm font-medium text-amber-900 dark:text-amber-100">
        System restarting — session creation will be available shortly
      </span>
    </div>
  )
}
