import { cn } from '@/lib/utils'
import { TIER_ACCENT, type StatusTier } from '@/session/statusTier'

/**
 * A session's status tier as a dot — the one rendering of it, shared by the session
 * card, a parent card's child rows and the Assistant drawer's "About:" line (D-12).
 * Colour and pulse come from `TIER_ACCENT`, the status → colour table. Decorative:
 * every surface that shows it also states the status or names the session in words.
 *
 * The pulse is `motion-safe:` (gated on `prefers-reduced-motion`); `still` turns it
 * off outright for a caller that already knows motion is reduced.
 */
export function SessionStatusDot({ tier, still = false, testId = 'session-status-dot' }: {
  tier: StatusTier
  still?: boolean
  testId?: string
}) {
  const accent = TIER_ACCENT[tier]
  return (
    <span
      data-testid={testId}
      aria-hidden="true"
      className={cn(
        'h-2 w-2 rounded-full flex-shrink-0',
        accent.dot,
        accent.pulse && !still && 'motion-safe:animate-pulse',
      )}
    />
  )
}
