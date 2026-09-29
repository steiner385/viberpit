import { Loader2 } from 'lucide-react'
import { Banner } from '@/ui/Banner'
import { useDaemonReachable } from '@/hooks/useDaemonReachable'
import { useEpochRestart } from '@/hooks/useEpochRestart'

/**
 * Non-modal banner shown while the session daemon is restarting or unreachable.
 *
 * Two triggers, and the copy names which (2026-09-29 re-critique: one word,
 * "Back in a moment…", for both a planned restart and an outage the app could
 * not explain — including a misconfigured proxy answering `/healthz` with HTML):
 *  - WS close code 1012 (epoch change / planned restart) → immediate via
 *    useEpochRestart. The daemon IS coming back; say so.
 *  - /healthz polling → catches unplanned outages via useDaemonReachable. The
 *    app knows only that the check failed and that it retries every 10 s; it
 *    does not know why, or whether sessions are affected, so it claims neither.
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
export const RESTARTING_COPY = 'Restarting the session daemon — back in a moment…'
export const UNREACHABLE_COPY = 'Can’t reach the session daemon — retrying every 10 s…'

export function DaemonRestartBanner() {
  const epochRestart = useEpochRestart()
  const daemonOk = useDaemonReachable()
  if (daemonOk && !epochRestart) return null
  return (
    <Banner
      tone="warning"
      title={epochRestart ? RESTARTING_COPY : UNREACHABLE_COPY}
      // motion-reduce: the one animation that survived reduced-motion emulation
      // in the re-critique's sweep was this spinner.
      icon={<Loader2 size={16} className="animate-spin motion-reduce:animate-none" aria-hidden="true" />}
      className="items-center"
    />
  )
}
