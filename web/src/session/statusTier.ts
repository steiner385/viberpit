import {
  Loader2, CircleAlert, HelpCircle, Eye, Check, CheckCircle2,
  AlertTriangle, X, Clock, Minimize2, Hourglass, RotateCcw,
  ListOrdered, UserCheck,
} from 'lucide-react'
import type { ComponentType } from 'react'
import type { SessionStatus } from '@/api/types'

export type StatusTier = 'busy' | 'needs-you' | 'your-turn' | 'waiting' | 'error'

export interface StatusPresentation {
  tier: StatusTier
  label: string
  Icon: ComponentType<{ className?: string; size?: number }>
  /** Tailwind text-color classes (light + dark). */
  textClass: string
}

const NEEDS_YOU = 'text-warning'
const ERROR = 'text-danger'
const BUSY = 'text-success'
const CALM = 'text-ink-muted'

// One table, keyed by the rich status string. 'working' is a tolerated legacy alias.
//
// Typed Record<SessionStatus | 'working', …> ON PURPOSE, not Record<string, …>: the
// status vocabulary used to live in two independent lists — the SessionStatus union in
// api/types.ts and these keys — with nothing tying them together. They drifted, and a
// status missing here does not fail loudly: it silently hits FALLBACK and renders as a
// calm "Ready". That is exactly how a not-yet-started 'queued' session came to look like
// a healthy idle one over an empty conversation. With this type, adding a member to
// SessionStatus fails the build until it has a pill here.
//
// NB the enforcing gate is CI's `npm run build` (`tsc -b`), NOT `npm run typecheck`:
// the root tsconfig is `"files": []` plus project references, so a bare `tsc --noEmit`
// against it checks zero files and passes no matter what.
const PRESENTATION: Record<SessionStatus | 'working', StatusPresentation> = {
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
  // Not started yet. These two carry no transcript, so the pill is the ONLY signal the
  // session has not run — a calm "Ready" over an empty conversation reads as a dead
  // session, which is exactly what this table's fallback used to produce for them.
  queued:           { tier: 'waiting',   label: 'Queued',            Icon: ListOrdered,   textClass: CALM },
  pending_approval: { tier: 'needs-you', label: 'Awaiting approval', Icon: UserCheck,     textClass: NEEDS_YOU },
}

const FALLBACK: StatusPresentation = {
  tier: 'your-turn', label: 'Ready', Icon: Clock, textClass: CALM,
}

// Issue #434: a session whose in-flight turn was dropped by a daemon wedge/restart comes
// back idle (rehydrate normalizes mid-turn → idle) — but that's not a calm "Ready", it needs
// the user to re-send to resume. Surface the cause + remedy directly as an actionable
// needs-you pill instead of a generic "Ready"/"Turn failed". Only overrides the resting
// states a restart can strand (idle / needs_attention), never a live/working turn.
const INTERRUPTED_BY_RESTART: StatusPresentation = {
  tier: 'needs-you', label: 'Interrupted — re-send to resume', Icon: RotateCcw, textClass: NEEDS_YOU,
}
const RESTART_STRANDABLE = new Set(['idle', 'needs_attention'])

// Presentation for a session lifted into the error tier by a failing PR (not by
// its own status) — see sessionTriage() in triageTier.ts. Lives here, beside
// PRESENTATION, so every "how does an error-tier session read?" decision has one
// home. The label names the *cause* so a card in the "Needs attention" group can
// never sit under a calm "Your turn"/"Ready" pill again.
export const PR_CHECKS_FAILING: StatusPresentation = {
  tier: 'error', label: 'Checks failing', Icon: AlertTriangle, textClass: ERROR,
}

export interface StatusPresentationOpts {
  /** The session's turn was dropped by a daemon restart (Session.interruptedByRestart). */
  interruptedByRestart?: boolean
}

// DIAGNOSTIC (temporary): any status string not in PRESENTATION renders as the
// calm "Ready" fallback — visually indistinguishable from a genuine idle session.
// Record each *novel* unknown string once so we can tell a transport/vocabulary
// gap (unrecognized state arrived) from a real backend idle/done. Deduped because
// statusPresentation() runs inside render/sort hot paths. Remove once the
// false-"Ready" question is settled.
const seenUnknownStatuses = new Set<string>()

export function statusPresentation(
  state: string, opts?: StatusPresentationOpts,
): StatusPresentation {
  if (opts?.interruptedByRestart && RESTART_STRANDABLE.has(state)) {
    return INTERRUPTED_BY_RESTART
  }
  // Cast to widen the key type for lookup only: `state` arrives as a plain string off
  // the wire, so an unknown value must still be *handled* (FALLBACK + the warn below)
  // even though the table itself is exhaustively typed over SessionStatus.
  const hit = (PRESENTATION as Record<string, StatusPresentation | undefined>)[state]
  if (!hit && !seenUnknownStatuses.has(state)) {
    seenUnknownStatuses.add(state)
    console.warn(`[status] unrecognized status %o rendered as fallback "Ready"`, state)
  }
  return hit ?? FALLBACK
}

export function statusTier(state: string): StatusTier {
  return statusPresentation(state).tier
}

/**
 * Per-tier accent colors for the session card's status rail and dot. Lives next
 * to the presentation table so status → color stays a single source of truth.
 * `pulse` marks the action-required tiers whose dot animates (motion-safe).
 */
export const TIER_TONE: Record<StatusTier, 'success' | 'warning' | 'danger' | 'neutral' | 'info'> = {
  busy: 'success', 'needs-you': 'warning', error: 'danger', 'your-turn': 'neutral', waiting: 'info',
}

export const TIER_ACCENT: Record<StatusTier, { rail: string; dot: string; pulse: boolean }> = {
  busy: { rail: 'bg-success', dot: 'bg-success', pulse: false },
  'needs-you': { rail: 'bg-warning', dot: 'bg-warning', pulse: true },
  error: { rail: 'bg-danger', dot: 'bg-danger', pulse: true },
  // Deliberately muted relative to the dot (see module docstring above).
  'your-turn': { rail: 'bg-neutral/40', dot: 'bg-neutral', pulse: false },
  waiting: { rail: 'bg-info/40', dot: 'bg-info', pulse: false },
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
