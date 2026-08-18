import { Loader2 } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useDaemonHealth } from '@/hooks/useDaemonHealth'
import { useEpochRestart } from '@/hooks/useEpochRestart'

/**
 * Non-modal sticky banner shown while the session daemon is restarting or unreachable.
 *
 * Two triggers:
 *  - WS close code 1012 (epoch change / planned restart) → immediate via useEpochRestart.
 *  - /healthz polling → catches unplanned outages via useDaemonHealth.
 *
 * Auto-clears once /healthz reports daemon healthy — no manual dismiss needed.
 * pointer-events-none so the fixed top-0 bar never intercepts clicks beneath it (#620).
 */
export function DaemonRestartBanner() {
  const epochRestart = useEpochRestart()
  const daemonOk = useDaemonHealth()
  if (daemonOk && !epochRestart) return null
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        // pointer-events-none: non-interactive status notice. Without it the fixed
        // top-0 bar overlays the session top bar and captures clicks meant for the
        // controls beneath it (e.g. "Back to session list" — #620 broke dashboard e2e T044).
        'pointer-events-none fixed inset-x-0 top-0 z-50 flex items-center justify-center gap-3',
        'border-b border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-900/90 px-4 py-2',
      )}
    >
      <Loader2 size={16} className="text-amber-700 dark:text-amber-300 flex-shrink-0 animate-spin" aria-hidden="true" />
      <span className="text-sm font-medium text-amber-900 dark:text-amber-100">
        Back in a moment…
      </span>
    </div>
  )
}
