import { Loader2 } from 'lucide-react'
import { Banner } from '@/ui/Banner'
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
    <Banner
      tone="warning"
      title="Back in a moment…"
      icon={<Loader2 size={16} className="animate-spin" aria-hidden="true" />}
      className={
        // pointer-events-none: non-interactive status notice. Without it the fixed
        // top-0 bar overlays the session top bar and captures clicks meant for the
        // controls beneath it (e.g. "Back to session list" — #620 broke dashboard e2e T044).
        'pointer-events-none fixed inset-x-0 top-0 z-50 items-center justify-center rounded-none border-x-0 border-t-0 px-4 py-2'
      }
    />
  )
}
