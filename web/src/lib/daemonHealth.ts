import { isValidTimestamp, relTime } from './relativeTime'
import type { DaemonHealth } from '@/api/types'

/** Compact human uptime: "3d", "5h", "12m", "45s". */
export function formatUptime(sec: number | null): string {
  if (sec == null || sec < 0) return '—'
  if (sec >= 86400) return `${Math.floor(sec / 86400)}d`
  if (sec >= 3600) return `${Math.floor(sec / 3600)}h`
  if (sec >= 60) return `${Math.floor(sec / 60)}m`
  return `${Math.floor(sec)}s`
}

/** The `title` tooltip text for the daemon-health indicator — uptime, restarts, epoch. */
export function daemonHealthTooltip(h: DaemonHealth): string {
  const lines = [`Daemon uptime: ${formatUptime(h.uptimeSec)}`, `Restarts (24h): ${h.restartCount24h}`]
  if (isValidTimestamp(h.lastRestartAt)) lines.push(`Last restart: ${relTime(h.lastRestartAt)}`)
  if (h.epoch) lines.push(`Epoch: ${h.epoch}`)
  return lines.join('\n')
}
