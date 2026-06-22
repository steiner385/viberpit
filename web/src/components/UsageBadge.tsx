import {
  CheckCircle2,
  AlertTriangle,
  WifiOff,
  CircleAlert,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { formatResetTime } from '@/lib/formatReset'
import { statusPresentation, type StatusPresentation } from '@/session/statusTier'
import type { AccountStatus, SessionStatus } from '@/api/types'

// ── UsageBadge ──────────────────────────────────────────────────────────────

interface UsageBadgeProps {
  pct: number
  label?: string  // e.g. "session" or "weekly"
  /** ISO timestamp the window resets at, or null when no window is active. */
  resetsAt?: string | null
  className?: string
}

export function UsageBadge({ pct, label, resetsAt, className }: UsageBadgeProps) {
  const clampedPct = Math.min(100, Math.max(0, pct))
  // Color classes supplementary only — text carries the info
  const barColor =
    clampedPct >= 90 ? 'bg-red-600 dark:bg-red-500' :
    clampedPct >= 70 ? 'bg-amber-600 dark:bg-amber-500' :
    'bg-emerald-600 dark:bg-emerald-500'

  const ariaLabel = label
    ? `${label} usage: ${clampedPct}%`
    : `Usage: ${clampedPct}%`

  // Tooltip is self-describing (it has no visible label next to it on hover);
  // the aria-label already names the window, so it only appends the reset phrase.
  const windowName = label ? `${label} limit` : 'Usage limit'
  const resetText = formatResetTime(resetsAt)
  const resetPhrase = resetText ? `resets ${resetText}` : 'no active window'
  const title = `${windowName} · ${resetPhrase}`

  return (
    <span
      className={cn('inline-flex flex-col gap-0.5', className)}
      title={title}
      aria-label={`${ariaLabel} — ${resetPhrase}`}
    >
      <span className="flex items-baseline gap-1 leading-none">
        {label && (
          <span className="text-[10px] font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
            {label}
          </span>
        )}
        <span className="text-xs font-medium tabular-nums text-gray-900 dark:text-gray-100">
          {clampedPct}%
        </span>
      </span>
      <span className="h-1 w-12 rounded-full bg-gray-200 dark:bg-gray-700 overflow-hidden">
        <span
          className={cn('h-full block rounded-full transition-all', barColor)}
          style={{ width: `${clampedPct}%` }}
          aria-hidden="true"
        />
      </span>
    </span>
  )
}

// ── StatusPill ───────────────────────────────────────────────────────────────

type AnyStatus = AccountStatus | SessionStatus

// Session statuses resolve through the single source of truth (statusTier);
// account statuses keep their own table here (they are account-level, not part
// of the session-status vocabulary). Both produce the same shape.
type StatusConfig = StatusPresentation

const ACCOUNT_STATUS_MAP: Record<AccountStatus, Omit<StatusConfig, 'tier'>> = {
  ok: {
    label: 'OK',
    Icon: CheckCircle2,
    textClass: 'text-emerald-700 dark:text-emerald-400',
  },
  limited: {
    label: 'Limited',
    Icon: AlertTriangle,
    textClass: 'text-amber-700 dark:text-amber-400',
  },
  'needs-relogin': {
    label: 'Needs login',
    Icon: WifiOff,
    textClass: 'text-red-700 dark:text-red-400',
  },
  'needs-reauth': {
    label: 'Needs re-auth',
    Icon: WifiOff,
    textClass: 'text-red-700 dark:text-red-400',
  },
  'not-max': {
    label: 'Not Max',
    Icon: CircleAlert,
    textClass: 'text-gray-600 dark:text-gray-400',
  },
  unknown: {
    label: 'Unknown',
    Icon: CircleAlert,
    textClass: 'text-gray-600 dark:text-gray-400',
  },
}

interface StatusPillProps {
  status: AnyStatus
  className?: string
}

/** Account statuses key the account-level table; everything else (the session
 *  vocabulary, its 'working' alias, and the limited/ended overlays) resolves
 *  through the shared statusTier source of truth. */
function resolveStatus(status: AnyStatus): Omit<StatusConfig, 'tier'> {
  if (status in ACCOUNT_STATUS_MAP) {
    return ACCOUNT_STATUS_MAP[status as AccountStatus]
  }
  return statusPresentation(status)
}

/** The needs-you and error tiers are operationally urgent; the list pulses them
 *  (respecting prefers-reduced-motion via the `motion-safe:` variant). */
function shouldPulse(status: AnyStatus): boolean {
  if (status in ACCOUNT_STATUS_MAP) return false
  const tier = statusPresentation(status).tier
  return tier === 'needs-you' || tier === 'error'
}

/** Subtle pill background derived from the status's text-colour family, so the
 *  badge reads as a real chip without maintaining a parallel colour table. */
function pillTone(textClass: string): string {
  if (textClass.includes('emerald')) return 'bg-emerald-50 dark:bg-emerald-950/40'
  if (textClass.includes('amber')) return 'bg-amber-50 dark:bg-amber-950/40'
  if (textClass.includes('red')) return 'bg-red-50 dark:bg-red-950/40'
  if (textClass.includes('blue')) return 'bg-blue-50 dark:bg-blue-950/40'
  return 'bg-gray-100 dark:bg-gray-800'
}

export function StatusPill({ status, className }: StatusPillProps) {
  const { label, Icon, textClass } = resolveStatus(status)

  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium',
        textClass,
        pillTone(textClass),
        // Loud, attention-grabbing pulse for the needs-you / error tiers; the
        // motion-safe variant drops it under prefers-reduced-motion.
        shouldPulse(status) && 'motion-safe:animate-pulse',
        className,
      )}
      aria-label={`Status: ${label}`}
    >
      <Icon size={12} aria-hidden="true" />
      <span>{label}</span>
    </span>
  )
}
