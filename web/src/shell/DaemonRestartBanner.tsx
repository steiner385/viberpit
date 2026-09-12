import { Loader2 } from 'lucide-react'
import { Banner } from '@/ui/Banner'
import { useDaemonReachable } from '@/hooks/useDaemonReachable'
import { useEpochRestart } from '@/hooks/useEpochRestart'

/**
 * Non-modal banner shown while the session daemon is restarting or unreachable.
 *
 * Two triggers:
 *  - WS close code 1012 (epoch change / planned restart) → immediate via useEpochRestart.
 *  - /healthz polling → catches unplanned outages via useDaemonReachable.
 *
 * Auto-clears once /healthz reports daemon healthy — no manual dismiss needed.
 *
 * Renders IN FLOW inside the shell's `BannerStack`, never `fixed`. It used to
 * be `fixed top-0 z-50`, which put it on the same screen band as the toaster
 * and the session top bar; the `pointer-events-none` hack that stopped it
 * eating the "Back to session list" click (#620) only existed because of that.
 * In flow it pushes content down instead of covering it, and there is nothing
 * beneath it to intercept.
 */
export function DaemonRestartBanner() {
  const epochRestart = useEpochRestart()
  const daemonOk = useDaemonReachable()
  if (daemonOk && !epochRestart) return null
  return (
    <Banner
      tone="warning"
      title="Back in a moment…"
      icon={<Loader2 size={16} className="animate-spin" aria-hidden="true" />}
      className="items-center"
    />
  )
}
