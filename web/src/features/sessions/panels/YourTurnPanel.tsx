import { MessageCircleReply } from 'lucide-react'
import { cn } from '@/lib/utils'

interface Props {
  /** Rich status string from the session runtime (statusTier vocabulary). */
  statusState: string
  /** Cached next-prompt suggestions (rt.state.latestSuggestions). */
  suggestions: string[]
  onPrompt: (text: string, attachmentIds: string[]) => void
}

/**
 * Pinned "Your turn" call-to-action above the composer. A finished turn ('done')
 * renders as the calmest pill in the app, yet it is exactly the state where the
 * user must act to keep the session moving — and the agent's actual ask is prose
 * buried at the bottom of the transcript scroller. Mirror the pinned
 * PendingQuestion/PendingPermission panels: say plainly that the session is
 * waiting, and promote the cached suggestions (otherwise hidden in the ghost
 * placeholder and the quick-reply dropdown) to visible one-tap replies.
 *
 * Keyed on 'done' only: 'idle'/'complete'/'ended' need no reply to make
 * progress, 'waiting_external' is the agent still working, and the needs-you
 * states already pin their own actionable panels.
 */
export function YourTurnPanel({ statusState, suggestions, onPrompt }: Props) {
  if (statusState !== 'done') return null
  return (
    <div
      role="region"
      aria-label="Your turn"
      aria-live="polite"
      className={cn(
        'flex-shrink-0 border-t border-accent',
        'bg-accent-wash/60 px-3 py-2',
      )}
      data-testid="your-turn-panel"
    >
      <div className="flex items-center gap-2 text-sm text-accent">
        <MessageCircleReply size={16} aria-hidden="true" className="flex-shrink-0" />
        <p>
          <span className="font-semibold">Your turn</span>
          <span> — reply to keep this session moving.</span>
        </p>
      </div>
      {suggestions.length > 0 ? (
        <div className="mt-2 flex flex-wrap gap-2">
          {suggestions.map((s, i) => (
            <button
              key={`${i}-${s.slice(0, 24)}`}
              type="button"
              onClick={() => onPrompt(s, [])}
              className={cn(
                'max-w-full truncate rounded-full border px-3 py-1.5 text-sm text-left',
                'border-accent bg-surface',
                'text-accent',
                'hover:bg-accent-wash',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
              )}
            >
              {s}
            </button>
          ))}
        </div>
      ) : (
        <p className="mt-1 pl-6 text-xs text-accent">
          Review the last message above, then reply below.
        </p>
      )}
    </div>
  )
}
