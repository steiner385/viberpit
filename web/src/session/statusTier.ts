import {
  Loader2, CircleAlert, HelpCircle, Eye, Check, CheckCircle2,
  AlertTriangle, X, Clock, Minimize2, Hourglass,
} from 'lucide-react'
import type { ComponentType } from 'react'

export type StatusTier = 'busy' | 'needs-you' | 'your-turn' | 'waiting' | 'error'

export interface StatusPresentation {
  tier: StatusTier
  label: string
  Icon: ComponentType<{ className?: string; size?: number }>
  /** Tailwind text-color classes (light + dark). */
  textClass: string
}

const NEEDS_YOU = 'text-amber-700 dark:text-amber-400'
const ERROR = 'text-red-700 dark:text-red-400'
const BUSY = 'text-emerald-700 dark:text-emerald-400'
const CALM = 'text-gray-600 dark:text-gray-400'

// One table, keyed by the rich status string. 'working' is a tolerated legacy alias.
const PRESENTATION: Record<string, StatusPresentation> = {
  in_progress:      { tier: 'busy',      label: 'Working…',          Icon: Loader2,       textClass: BUSY },
  working:          { tier: 'busy',      label: 'Working…',          Icon: Loader2,       textClass: BUSY },
  retrying:         { tier: 'busy',      label: 'Overloaded — retrying', Icon: Loader2,   textClass: NEEDS_YOU },
  condensing:       { tier: 'busy',      label: 'Condensing…',       Icon: Minimize2,     textClass: BUSY },
  ready_review:     { tier: 'needs-you', label: 'Approve plan',      Icon: Eye,           textClass: NEEDS_YOU },
  waiting_input:    { tier: 'needs-you', label: 'Permission needed', Icon: CircleAlert,   textClass: NEEDS_YOU },
  waiting_question: { tier: 'needs-you', label: 'Answer needed',     Icon: HelpCircle,    textClass: NEEDS_YOU },
  done:             { tier: 'your-turn', label: 'Your turn',         Icon: Check,         textClass: CALM },
  complete:         { tier: 'your-turn', label: 'Complete',          Icon: CheckCircle2,  textClass: CALM },
  idle:             { tier: 'your-turn', label: 'Ready',             Icon: Clock,         textClass: CALM },
  waiting_external: { tier: 'waiting',   label: 'Waiting…',          Icon: Hourglass,     textClass: CALM },
  needs_attention:  { tier: 'error',     label: 'Turn failed',       Icon: AlertTriangle, textClass: ERROR },
  limited:          { tier: 'error',     label: 'Usage limit',       Icon: AlertTriangle, textClass: ERROR },
  ended:            { tier: 'your-turn', label: 'Ended',             Icon: X,             textClass: CALM },
}

const FALLBACK: StatusPresentation = {
  tier: 'your-turn', label: 'Ready', Icon: Clock, textClass: CALM,
}

// DIAGNOSTIC (temporary): any status string not in PRESENTATION renders as the
// calm "Ready" fallback — visually indistinguishable from a genuine idle session.
// Record each *novel* unknown string once so we can tell a transport/vocabulary
// gap (unrecognized state arrived) from a real backend idle/done. Deduped because
// statusPresentation() runs inside render/sort hot paths. Remove once the
// false-"Ready" question is settled.
const seenUnknownStatuses = new Set<string>()

export function statusPresentation(state: string): StatusPresentation {
  const hit = PRESENTATION[state]
  if (!hit && !seenUnknownStatuses.has(state)) {
    seenUnknownStatuses.add(state)
    console.warn(`[status] unrecognized status %o rendered as fallback "Ready"`, state)
  }
  return hit ?? FALLBACK
}

export function statusTier(state: string): StatusTier {
  return statusPresentation(state).tier
}

export const TIER_RANK: Record<StatusTier, number> = {
  'needs-you': 0, error: 1, 'your-turn': 2, waiting: 3, busy: 4,
}

/**
 * Human-readable section header for each tier (group-by-status). These name the
 * tier as a category and intentionally avoid reusing any individual status pill
 * label: the 'your-turn' tier bundles several pills (Ready / Your turn / Complete
 * / Ended), so its header is "Done" rather than "Your turn" — otherwise a header
 * named "Your turn" would sit above "Ready" cards and read as misfiled.
 */
export const TIER_LABEL: Record<StatusTier, string> = {
  'needs-you': 'Needs you',
  error: 'Needs attention',
  'your-turn': 'Done',
  waiting: 'Waiting',
  busy: 'Working',
}

/** Stable sort by status tier: action-required first, busy last. Returns a new array. */
export function sortSessionsByTier<T extends { status: string }>(items: readonly T[]): T[] {
  return [...items].sort((a, b) => TIER_RANK[statusTier(a.status)] - TIER_RANK[statusTier(b.status)])
}
