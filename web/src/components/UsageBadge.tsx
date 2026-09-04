import {
  CheckCircle2,
  AlertTriangle,
  WifiOff,
  CircleAlert,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { formatResetTime } from '@/lib/formatReset'
import type { AccountStatus, SessionStatus } from '@/api/types'
import { statusPresentation, TIER_TONE } from '@/session/statusTier'
import { StatusPill as Pill } from '@/ui/StatusPill'
import type { Tone } from '@/ui/Badge'

// ── UsageBadge ──────────────────────────────────────────────────────────────

interface UsageBadgeProps {
  pct: number
  label?: string  // e.g. "session" or "weekly"
  /** ISO timestamp the window resets at, or null when no window is active. */
  resetsAt?: string | null
  /** Reserve marker position (0–100); renders a vertical line on the bar when set. */
  markerPct?: number | null
  /** Dense inline variant: micro-bar + % on one line, no uppercase label (the
   *  label still names the window in the tooltip/aria-label). */
  compact?: boolean
  className?: string
}

export function UsageBadge({ pct, label, resetsAt, markerPct, compact, className }: UsageBadgeProps) {
  const clampedPct = Math.min(100, Math.max(0, pct))
  // Color classes supplementary only — text carries the info
  const barColor =
    clampedPct >= 90 ? 'bg-danger' :
    clampedPct >= 70 ? 'bg-warning' :
    'bg-success'

  const ariaLabel = label
    ? `${label} usage: ${clampedPct}%`
    : `Usage: ${clampedPct}%`

  // Tooltip is self-describing (it has no visible label next to it on hover);
  // the aria-label already names the window, so it only appends the reset phrase.
  const windowName = label ? `${label} limit` : 'Usage limit'
  const resetText = formatResetTime(resetsAt)
  const resetPhrase = resetText ? `resets ${resetText}` : 'no active window'
  const title = `${windowName} · ${resetPhrase}`

  if (compact) {
    return (
      <span
        className={cn('items-center gap-1', className)}
        title={title}
        aria-label={`${ariaLabel} — ${resetPhrase}`}
      >
        <span className="relative h-1 w-10 rounded-full bg-surface-raised overflow-hidden">
          <span
            className={cn('h-full block rounded-full', barColor)}
            style={{ width: `${clampedPct}%` }}
            aria-hidden="true"
          />
        </span>
        <span className="text-[10px] font-medium tabular-nums text-ink-muted">
          {clampedPct}%
        </span>
      </span>
    )
  }

  return (
    <span
      className={cn('inline-flex flex-col gap-0.5', className)}
      title={title}
      aria-label={`${ariaLabel} — ${resetPhrase}`}
    >
      <span className="flex items-baseline gap-1 leading-none">
        {label && (
          <span className="text-[10px] font-medium uppercase tracking-wide text-ink-muted">
            {label}
          </span>
        )}
        <span className="text-xs font-medium tabular-nums text-ink">
          {clampedPct}%
        </span>
      </span>
      <span className="relative h-1 w-12 rounded-full bg-surface-raised overflow-hidden">
        <span
          className={cn('h-full block rounded-full transition-all', barColor)}
          style={{ width: `${clampedPct}%` }}
          aria-hidden="true"
        />
        {markerPct != null && (
          <span
            className="absolute top-0 h-full w-px bg-ink/70"
            style={{ left: `${Math.min(100, Math.max(0, markerPct))}%` }}
            data-testid="reserve-marker"
            aria-hidden="true"
          />
        )}
      </span>
    </span>
  )
}

// ── StatusPill ───────────────────────────────────────────────────────────────
// Thin adapter over the shared ui/StatusPill primitive: this module keeps its
// own status vocabulary (account statuses, session statuses) but hands the
// chrome — pill shape, tone colors, pulse — to the single primitive.

type AnyStatus = AccountStatus | SessionStatus

interface StatusConfig {
  label: string
  Icon: React.ComponentType<{ className?: string; size?: number }>
  tone: Tone
}

// Account statuses have their own vocabulary (ok/limited/needs-reauth/…),
// mapped onto the shared Tone set rather than a parallel colour table.
const ACCOUNT_STATUS_MAP: Record<AccountStatus, StatusConfig> = {
  ok: {
    label: 'OK',
    Icon: CheckCircle2,
    tone: 'success',
  },
  limited: {
    label: 'Limited',
    Icon: AlertTriangle,
    tone: 'warning',
  },
  'needs-relogin': {
    label: 'Needs login',
    Icon: WifiOff,
    tone: 'danger',
  },
  'needs-reauth': {
    label: 'Needs re-auth',
    Icon: WifiOff,
    tone: 'danger',
  },
  'not-max': {
    label: 'Not Max',
    Icon: CircleAlert,
    tone: 'neutral',
  },
  unknown: {
    label: 'Unknown',
    Icon: CircleAlert,
    tone: 'neutral',
  },
}

interface StatusPillProps {
  status: AnyStatus
  className?: string
  /** Issue #434: relabel a restart-stranded session as "re-send to resume". */
  interruptedByRestart?: boolean
  /** Server-derived Session.usageStalled — parked for lack of usage headroom. */
  usageStalled?: boolean
  /** Session.pausedUntil — earliest known reset, for the pill's time suffix. */
  pausedUntil?: string | null
}

export function StatusPill({ status, className, interruptedByRestart, usageStalled, pausedUntil }: StatusPillProps) {
  // Account statuses have their own vocabulary; session statuses use the shared map.
  const account = (ACCOUNT_STATUS_MAP as Record<string, StatusConfig>)[status]
  if (account) {
    return <Pill label={account.label} Icon={account.Icon} tone={account.tone} className={className} />
  }
  const p = statusPresentation(status, { interruptedByRestart, usageStalled, pausedUntil })
  return (
    <Pill
      label={p.label}
      Icon={p.Icon}
      tone={TIER_TONE[p.tier]}
      pulse={p.tier === 'needs-you' || p.tier === 'error'}
      className={className}
    />
  )
}
