import type { LostHold } from '@/api/types'
import { cn } from '@/lib/utils'
import { askAgainPrompt, lostHoldHeading, lostHoldLines } from './lostHold'

interface Props {
  lostHold: LostHold
  /** Called with the ready-to-send recovery prompt (`askAgainPrompt`), so the caller
   *  needs nothing from `lostHold.ts` and this module can stay off the initial path. */
  onAskAgain: (prompt: string) => void
}

/**
 * A hold (permission, question or plan approval) whose waiter died with the daemon,
 * shown WITH its content (spec 002 FR-406). Same visual language as
 * `InterruptedAskNotice`, which remains the fallback when no content survived. The
 * only control is "Ask again": there is no approve / deny / answer here, because a
 * lost hold cannot be decided after the fact (FR-306).
 *
 * Lazy: `SessionBody` imports it through `import()` so the banner and its wording
 * module ship only to a session that has a lost hold.
 */
export default function LostHoldBanner({ lostHold, onAskAgain }: Props) {
  const lines = lostHoldLines(lostHold)
  return (
    <div
      data-testid="lost-hold-banner"
      className={cn(
        'flex-shrink-0 border-t border-warning',
        'bg-warning-wash/60 px-3 py-2 text-xs',
        'text-warning flex items-start gap-3',
      )}
    >
      {/* The live region is the COPY, not the wrapper (see InterruptedAskNotice). */}
      <div role="status" className="min-w-0 flex-1">
        <p>When the daemon restarted, the agent was waiting on a {lostHoldHeading(lostHold)}:</p>
        {lines.length > 0 && (
          <ul className="mt-1 space-y-0.5 text-ink">
            {lines.map((line, i) => (
              <li key={i} className="whitespace-pre-wrap break-words font-mono">{line}</li>
            ))}
          </ul>
        )}
      </div>
      <button
        type="button"
        onClick={() => onAskAgain(askAgainPrompt(lostHold))}
        className={cn(
          'flex-shrink-0 rounded border border-warning',
          'px-2 py-1 font-medium hover:bg-warning-wash',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-warning',
        )}
      >
        Ask again
      </button>
    </div>
  )
}
