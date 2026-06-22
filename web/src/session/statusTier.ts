import {
  Loader2, CircleAlert, HelpCircle, Eye, Check, CheckCircle2,
  AlertTriangle, X, Clock,
} from 'lucide-react'
import type { ComponentType } from 'react'

export type StatusTier = 'busy' | 'needs-you' | 'your-turn' | 'error'

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
  ready_review:     { tier: 'needs-you', label: 'Approve plan',      Icon: Eye,           textClass: NEEDS_YOU },
  waiting_input:    { tier: 'needs-you', label: 'Permission needed', Icon: CircleAlert,   textClass: NEEDS_YOU },
  waiting_question: { tier: 'needs-you', label: 'Answer needed',     Icon: HelpCircle,    textClass: NEEDS_YOU },
  done:             { tier: 'your-turn', label: 'Your turn',         Icon: Check,         textClass: CALM },
  complete:         { tier: 'your-turn', label: 'Your turn',         Icon: CheckCircle2,  textClass: CALM },
  idle:             { tier: 'your-turn', label: 'Ready',             Icon: Clock,         textClass: CALM },
  needs_attention:  { tier: 'error',     label: 'Turn failed',       Icon: AlertTriangle, textClass: ERROR },
  limited:          { tier: 'error',     label: 'Usage limit',       Icon: AlertTriangle, textClass: ERROR },
  ended:            { tier: 'your-turn', label: 'Ended',             Icon: X,             textClass: CALM },
}

const FALLBACK: StatusPresentation = {
  tier: 'your-turn', label: 'Ready', Icon: Clock, textClass: CALM,
}

export function statusPresentation(state: string): StatusPresentation {
  return PRESENTATION[state] ?? FALLBACK
}

export function statusTier(state: string): StatusTier {
  return statusPresentation(state).tier
}

export const TIER_RANK: Record<StatusTier, number> = {
  'needs-you': 0, error: 1, 'your-turn': 2, busy: 3,
}

/** Stable sort by status tier: action-required first, busy last. Returns a new array. */
export function sortSessionsByTier<T extends { status: string }>(items: readonly T[]): T[] {
  return [...items].sort((a, b) => TIER_RANK[statusTier(a.status)] - TIER_RANK[statusTier(b.status)])
}
