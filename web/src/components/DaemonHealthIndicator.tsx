import { Activity, AlertTriangle } from 'lucide-react'
import { useDaemonHealth } from '@/api/hooks'
import { formatUptime, daemonHealthTooltip } from '@/lib/daemonHealth'
import { cn } from '@/lib/utils'

/**
 * Issue #434: daemon lifecycle health at a glance. A wedge → watchdog-restart used to be
 * invisible (log archaeology only). This surfaces uptime always and, when the daemon has
 * restarted in the last 24h, an amber/red count — 9 restarts/day (the 2026-07-02 incident)
 * is abnormal and worth alerting on even after #433 made restarts non-destructive.
 *
 * Hidden entirely when there's no daemon to watch (in-process dev mode → available:false).
 *
 * The "daemon restarted" toast was removed (#661): the DaemonRestartBanner overlay fires
 * immediately on WS close-1012 and covers the restart recovery UX; a duplicate post-hoc
 * toast adds noise.
 */
export function DaemonHealthIndicator() {
  const { data } = useDaemonHealth()

  if (!data?.available) return null

  const flapping = data.restartCount24h > 0
  const Icon = flapping ? AlertTriangle : Activity
  const label = flapping
    ? `${data.restartCount24h}× restart${data.restartCount24h === 1 ? '' : 's'} (24h)`
    : `up ${formatUptime(data.uptimeSec)}`

  return (
    <span
      data-testid="daemon-health"
      title={daemonHealthTooltip(data)}
      aria-label={`Daemon health: ${label}`}
      className={cn(
        'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium',
        flapping
          ? 'text-amber-700 dark:text-amber-400 bg-amber-500/10'
          : 'text-gray-500 dark:text-gray-400',
      )}
    >
      <Icon size={12} aria-hidden="true" className={flapping ? 'motion-safe:animate-pulse' : undefined} />
      <span>{label}</span>
    </span>
  )
}
