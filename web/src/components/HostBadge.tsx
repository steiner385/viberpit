/**
 * Which fleet host is running a session.
 *
 * Renders nothing when `host` is null: sessions created before the fleet existed
 * carry no host, and an "unknown" chip on every historical card is noise rather
 * than information.
 *
 * `healthy` is tri-state. Only an explicit `false` warns — null/undefined means
 * "not yet probed", and warning on that would cry wolf on every freshly
 * registered host.
 */
export function HostBadge({ host, healthy }: { host?: string | null; healthy?: boolean | null }) {
  if (!host) return null

  const unhealthy = healthy === false
  const label = unhealthy ? `${host} — host unhealthy` : `host ${host}`

  return (
    <span
      aria-label={label}
      title={label}
      className={
        'inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs font-medium ' +
        (unhealthy
          ? 'bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300'
          : 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300')
      }
    >
      <span aria-hidden="true">{unhealthy ? '⚠' : '▪'}</span>
      {host}
    </span>
  )
}
