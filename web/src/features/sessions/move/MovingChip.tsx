/**
 * "Moving to <host>…" (D-17) — shown on the session card and in the session header
 * while `movingTo` is set (a move saga is running, or a failed one is waiting for the
 * edge's reconcile). The host name is untrusted text: rendered as a plain string and
 * truncated, so a long one cannot push a 390px header or card sideways.
 */
import { cn } from '@/lib/utils'
import { movingLabel } from '@/api/sessionMove'

export function MovingChip({ host, className }: { host: string; className?: string }) {
  const label = movingLabel(host)
  return (
    <span
      data-testid="moving-chip"
      title={label}
      className={cn(
        'inline-flex min-w-0 max-w-[10rem] rounded px-1.5 py-0.5',
        'bg-info-wash text-info text-xs font-medium',
        className,
      )}
    >
      <span className="truncate">{label}</span>
    </span>
  )
}
