import { HandHelping, MessageCircleReply } from 'lucide-react'
import { cn } from '@/lib/utils'

type Resolution = 'confirm' | 'reject'

interface Props {
  /** Rich status string from the session runtime (statusTier vocabulary). */
  statusState: string
  /** Next-prompt suggestions (rt.suggestions — live event, else the record cache). */
  suggestions: string[]
  onPrompt: (text: string, attachmentIds: string[]) => void
  /** The one sentence the session passed to `hand_back(reason)` (Session.handBackReason).
   *  Read only while `statusState === 'handed_back'` — a resumed session keeps the
   *  stale row value, exactly the gate the list card applies. */
  handBackReason?: string | null
  /** The server's "likely complete" reading of this hand-back (Session.suspectedCompleteReason).
   *  Same status gate. A hint with two one-tap answers, never an automatic archive. */
  suspectedCompleteReason?: string | null
  /** Answer the hint: 'confirm' archives as complete, 'reject' drops it. Omitted =
   *  the caller has no mutation to offer (render the hint read-only). */
  onResolveSuspectedComplete?: (decision: Resolution) => void
  /** True while that answer is in flight; both buttons disable. */
  resolvePending?: boolean
}

/**
 * Pinned call-to-action above the composer for the two states where the agent
 * has stopped and the next move is the user's.
 *
 * 'done' — a finished turn renders as the calmest pill in the app, yet it is
 * exactly the state where the user must act to keep the session moving, and the
 * agent's actual ask is prose buried at the bottom of the transcript scroller.
 * Mirror the pinned PendingQuestion/PendingPermission panels: say plainly that
 * the session is waiting, and promote the cached suggestions (otherwise hidden in
 * the ghost placeholder and the quick-reply dropdown) to visible one-tap replies.
 *
 * 'handed_back' — the needs-you state with no card to answer (#2321). The session
 * said what it wants in one sentence via hand_back(reason); the list card shows
 * it, but inside the session nothing was pinned, so the one routine "answer what
 * needs me" item cost a full-transcript read (2026-09-29 critique, P0). Lead with
 * the reason in the needs-you tone, keep the same one-tap replies, and carry the
 * card's "Likely complete → Mark complete / Not done" so the decision can be
 * taken where the evidence is.
 *
 * Other states render nothing: 'idle'/'complete'/'ended' need no reply to make
 * progress, 'waiting_external' is the agent still working, and the remaining
 * needs-you states already pin their own actionable panels.
 */
export function YourTurnPanel({
  statusState, suggestions, onPrompt,
  handBackReason, suspectedCompleteReason, onResolveSuspectedComplete, resolvePending = false,
}: Props) {
  const handedBack = statusState === 'handed_back'
  if (statusState !== 'done' && !handedBack) return null

  // Tone follows statusTier: 'done' is the calm your-turn tier (accent CTA);
  // 'handed_back' is the needs-you tier (warning), matching its pill and card rail.
  const tone = handedBack
    ? { border: 'border-warning', wash: 'bg-warning-wash/60', text: 'text-warning', ring: 'focus-visible:ring-warning', hover: 'hover:bg-warning-wash' }
    : { border: 'border-accent', wash: 'bg-accent-wash/60', text: 'text-accent', ring: 'focus-visible:ring-accent', hover: 'hover:bg-accent-wash' }
  const Icon = handedBack ? HandHelping : MessageCircleReply
  const reason = handedBack ? handBackReason?.trim() || null : null
  const suspected = handedBack ? suspectedCompleteReason?.trim() || null : null

  return (
    <div
      role="region"
      aria-label={handedBack ? 'Handed back' : 'Your turn'}
      aria-live="polite"
      className={cn('flex-shrink-0 border-t px-3 py-2', tone.border, tone.wash)}
      data-testid="your-turn-panel"
      data-mode={handedBack ? 'handed_back' : 'done'}
    >
      <div className={cn('flex items-start gap-2 text-sm', tone.text)}>
        <Icon size={16} aria-hidden="true" className="mt-0.5 flex-shrink-0" />
        <p className="min-w-0">
          {handedBack ? (
            <>
              <span className="font-semibold">Handed back</span>
              {/* The session's own sentence is the one fact the user needs now; it
                  reads as prose, not a truncated meta clause, so a long ask wraps. */}
              <span> — {reason ?? 'the session stopped and is waiting on you.'}</span>
            </>
          ) : (
            <>
              <span className="font-semibold">Your turn</span>
              <span> — reply to keep this session moving.</span>
            </>
          )}
        </p>
      </div>

      {suspected && (
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 pl-6 text-sm">
          <span className="text-success">
            <span className="font-medium">Likely complete</span>
            <span className="text-ink-muted"> — {suspected}</span>
          </span>
          {onResolveSuspectedComplete && (
            <span className="inline-flex gap-2">
              <button
                type="button"
                disabled={resolvePending}
                onClick={() => onResolveSuspectedComplete('confirm')}
                title={`Archive as complete — ${suspected}`}
                className={cn(
                  'min-h-target rounded-full border border-success/60 bg-surface px-3 py-1 text-sm font-medium text-success',
                  'hover:bg-success/15 disabled:opacity-60 disabled:pointer-events-none',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
                )}
              >
                Mark complete
              </button>
              <button
                type="button"
                disabled={resolvePending}
                onClick={() => onResolveSuspectedComplete('reject')}
                title="Keep it handed back; drop the hint"
                className={cn(
                  'min-h-target rounded-full border border-edge-strong bg-surface px-3 py-1 text-sm font-medium text-ink-muted',
                  'hover:bg-surface-raised disabled:opacity-60 disabled:pointer-events-none',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
                )}
              >
                Not done
              </button>
            </span>
          )}
        </div>
      )}

      {suggestions.length > 0 ? (
        <div className="mt-2 flex flex-wrap gap-2">
          {suggestions.map((s, i) => (
            <button
              key={`${i}-${s.slice(0, 24)}`}
              type="button"
              onClick={() => onPrompt(s, [])}
              className={cn(
                // min-h-target: the 44px house tap target (QuestionCard's option
                // buttons use the same class) — these are one-tap sends on a phone.
                'min-h-target max-w-full truncate rounded-full border px-3 py-1.5 text-sm text-left',
                'bg-surface', tone.border, tone.text, tone.hover,
                'focus-visible:outline-none focus-visible:ring-2', tone.ring,
              )}
            >
              {s}
            </button>
          ))}
        </div>
      ) : (
        <p className={cn('mt-1 pl-6 text-xs', tone.text)}>
          {handedBack
            ? 'Read the last message above, then reply below.'
            : 'Review the last message above, then reply below.'}
        </p>
      )}
    </div>
  )
}
