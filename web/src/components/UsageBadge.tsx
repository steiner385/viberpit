import {
  Check,
  CheckCircle2,
  AlertTriangle,
  WifiOff,
  CircleAlert,
  Clock,
  X,
  Eye,
  Loader2,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import type { AccountStatus, SessionStatus } from '@/api/types'

// ── UsageBadge ──────────────────────────────────────────────────────────────

interface UsageBadgeProps {
  pct: number
  label?: string  // e.g. "session" or "weekly"
  className?: string
}

export function UsageBadge({ pct, label, className }: UsageBadgeProps) {
  const clampedPct = Math.min(100, Math.max(0, pct))
  // Color classes supplementary only — text carries the info
  const barColor =
    clampedPct >= 90 ? 'bg-red-600 dark:bg-red-500' :
    clampedPct >= 70 ? 'bg-amber-600 dark:bg-amber-500' :
    'bg-emerald-600 dark:bg-emerald-500'

  const ariaLabel = label
    ? `${label} usage: ${clampedPct}%`
    : `Usage: ${clampedPct}%`

  return (
    <span
      className={cn('inline-flex flex-col gap-0.5', className)}
      aria-label={ariaLabel}
    >
      <span className="text-xs font-medium tabular-nums leading-none text-gray-900 dark:text-gray-100">
        {clampedPct}%
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

interface StatusConfig {
  label: string
  Icon: React.ComponentType<{ className?: string; size?: number }>
  textClass: string
}

const ACCOUNT_STATUS_MAP: Record<AccountStatus, StatusConfig> = {
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

// Keyed by string (not SessionStatus) so the legacy live-event 'working' alias can
// coexist with the richer dashboard vocabulary without a type error.
const SESSION_STATUS_MAP: Record<string, StatusConfig> = {
  idle: {
    label: 'Idle',
    Icon: Clock,
    textClass: 'text-gray-600 dark:text-gray-400',
  },
  in_progress: {
    label: 'In progress',
    Icon: Loader2,
    textClass: 'text-emerald-700 dark:text-emerald-400',
  },
  // Alias: the in-session StatusBar still emits the live 'working' state.
  working: {
    label: 'In progress',
    Icon: Loader2,
    textClass: 'text-emerald-700 dark:text-emerald-400',
  },
  waiting_input: {
    label: 'Waiting for input',
    Icon: CircleAlert,
    textClass: 'text-amber-700 dark:text-amber-400',
  },
  ready_review: {
    label: 'Ready for review',
    Icon: Eye,
    textClass: 'text-blue-700 dark:text-blue-400',
  },
  done: {
    label: 'Done',
    Icon: Check,
    textClass: 'text-gray-600 dark:text-gray-400',
  },
  complete: {
    label: 'Complete',
    Icon: CheckCircle2,
    textClass: 'text-emerald-700 dark:text-emerald-400',
  },
  needs_attention: {
    label: 'Needs attention',
    Icon: AlertTriangle,
    textClass: 'text-amber-700 dark:text-amber-400',
  },
  limited: {
    label: 'Limited',
    Icon: AlertTriangle,
    textClass: 'text-amber-700 dark:text-amber-400',
  },
  ended: {
    label: 'Ended',
    Icon: X,
    textClass: 'text-gray-600 dark:text-gray-400',
  },
}

const ALL_STATUS_MAP: Record<string, StatusConfig> = {
  ...ACCOUNT_STATUS_MAP,
  ...SESSION_STATUS_MAP,
}

interface StatusPillProps {
  status: AnyStatus
  className?: string
}

export function StatusPill({ status, className }: StatusPillProps) {
  const config = ALL_STATUS_MAP[status] ?? {
    label: status,
    Icon: CircleAlert,
    textClass: 'text-gray-600 dark:text-gray-400',
  }
  const { label, Icon, textClass } = config

  return (
    <span
      className={cn('inline-flex items-center gap-1 text-xs font-medium', textClass, className)}
      aria-label={`Status: ${label}`}
    >
      <Icon size={12} aria-hidden="true" />
      <span>{label}</span>
    </span>
  )
}
