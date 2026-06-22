/**
 * Single source of truth for session-status presentation.
 *
 * The backend emits a RICH status vocabulary (in_progress / waiting_input /
 * ready_review / done / complete / needs_attention / idle), plus the
 * orchestrator overlays (limited / ended) which arrive only via the persisted
 * session record. Both the in-session StatusBar and the session-list pill map
 * those statuses through THIS module — there must be no parallel status→label
 * or status→colour table anywhere else (Constitution Principle VI).
 *
 * Four operational tiers collapse the vocabulary into the distinction that
 * actually matters at a glance:
 *   - busy       — the agent is actively working (neutral; animated indicator).
 *   - needs-you  — blocked on the user (permission / question / plan); LOUD amber.
 *   - your-turn  — turn finished / idle; the user MAY act but nothing is blocked.
 *   - error      — failed turn, usage-limited, or ended; LOUD red.
 */
import {
  Check,
  CheckCircle2,
  AlertTriangle,
  CircleAlert,
  Clock,
  X,
  Eye,
  Loader2,
} from 'lucide-react'

export type StatusTier = 'busy' | 'needs-you' | 'your-turn' | 'error'

export interface StatusPresentation {
  tier: StatusTier
  label: string
  Icon: React.ComponentType<{ className?: string; size?: number }>
  /** Text-colour class family; the pill background tone is derived from it. */
  textClass: string
}

/**
 * The presentation table. Keyed by string so the legacy live-event alias
 * ('working') can coexist with the rich vocabulary without a type error, and so
 * the orchestrator overlays ('limited' / 'ended') — which are not produced by
 * derive_status but arrive via the persisted record — still classify correctly.
 */
export const STATUS_PRESENTATION: Record<string, StatusPresentation> = {
  // ── busy ────────────────────────────────────────────────────────────────
  in_progress: {
    tier: 'busy',
    label: 'Working…',
    Icon: Loader2,
    textClass: 'text-emerald-700 dark:text-emerald-400',
  },
  // Legacy alias: older clients / events may still carry the binary 'working'.
  working: {
    tier: 'busy',
    label: 'Working…',
    Icon: Loader2,
    textClass: 'text-emerald-700 dark:text-emerald-400',
  },

  // ── needs-you (LOUD amber) ────────────────────────────────────────────────
  waiting_input: {
    tier: 'needs-you',
    label: 'Permission needed',
    Icon: CircleAlert,
    textClass: 'text-amber-700 dark:text-amber-400',
  },
  ready_review: {
    tier: 'needs-you',
    label: 'Approve plan',
    Icon: Eye,
    textClass: 'text-amber-700 dark:text-amber-400',
  },

  // ── your-turn (calm) ──────────────────────────────────────────────────────
  done: {
    tier: 'your-turn',
    label: 'Your turn',
    Icon: Check,
    textClass: 'text-emerald-700 dark:text-emerald-400',
  },
  complete: {
    tier: 'your-turn',
    label: 'Your turn',
    Icon: CheckCircle2,
    textClass: 'text-emerald-700 dark:text-emerald-400',
  },
  idle: {
    // A never-ran session reads "Ready"; once a turn completes the status is
    // 'done'/'complete' → "Your turn". Both are the calm tier.
    tier: 'your-turn',
    label: 'Ready',
    Icon: Clock,
    textClass: 'text-gray-600 dark:text-gray-400',
  },

  // ── error (LOUD red) ──────────────────────────────────────────────────────
  needs_attention: {
    tier: 'error',
    label: 'Turn failed',
    Icon: AlertTriangle,
    textClass: 'text-red-700 dark:text-red-400',
  },
  limited: {
    tier: 'error',
    label: 'Usage limit',
    Icon: AlertTriangle,
    textClass: 'text-red-700 dark:text-red-400',
  },
  ended: {
    tier: 'error',
    label: 'Ended',
    Icon: X,
    textClass: 'text-red-700 dark:text-red-400',
  },
}

/** Fallback for an unrecognised status — treated as the calm tier. */
const FALLBACK: StatusPresentation = {
  tier: 'your-turn',
  label: 'Ready',
  Icon: Clock,
  textClass: 'text-gray-600 dark:text-gray-400',
}

/** Resolve the full presentation for a status string (never throws). */
export function statusPresentation(state: string): StatusPresentation {
  return STATUS_PRESENTATION[state] ?? FALLBACK
}

/** Resolve only the operational tier for a status string. */
export function statusTier(state: string): StatusTier {
  return statusPresentation(state).tier
}

// ── Tier sort ─────────────────────────────────────────────────────────────

/**
 * Sort weight for a tier: needs-you and error float to the top (the user must
 * act), your-turn next, busy last. needs-you and error share weight 0 so they
 * cluster together at the top without reordering relative to each other.
 */
const TIER_RANK: Record<StatusTier, number> = {
  'needs-you': 0,
  error: 0,
  'your-turn': 1,
  busy: 2,
}

export function tierRank(state: string): number {
  return TIER_RANK[statusTier(state)]
}

/**
 * Stable sort of items by their status tier. Preserves the input order within a
 * tier (Array.prototype.sort is stable per spec), so cards only move when their
 * tier rank changes — callers should pass an already-stable list (e.g. the REST
 * order) to avoid jitter on every poll.
 */
export function sortByTier<T>(items: readonly T[], statusOf: (item: T) => string): T[] {
  return [...items].sort((a, b) => tierRank(statusOf(a)) - tierRank(statusOf(b)))
}
