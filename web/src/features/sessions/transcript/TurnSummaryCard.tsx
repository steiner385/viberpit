import type { PromptContext } from '@/session/events'
import { diffTotals, type ChangedFile, type SummaryOutcome } from '@/session/turnSummary'
import { DiffCounts } from '@/features/sessions/diff/DiffCounts'
import { cn } from '@/lib/utils'

/** Paths listed before the rest fold into "+K more". */
const MAX_PATHS = 5

type Resolution = 'confirm' | 'reject'

/** What the live session hands the transcript so its tail can carry the card. */
export interface TurnSummaryInputs {
  /** Rich status string from the session runtime (statusTier vocabulary). */
  status: string
  /** Session.handBackReason — `summaryOutcome` decides where it is read. */
  handBackReason?: string | null
  /** Session.suspectedCompleteReason — the server's "likely complete" hint. */
  suspectedCompleteReason?: string | null
  /** Next-prompt suggestions (rt.suggestions — live event, else the record cache). */
  suggestions: string[]
  onPrompt: (text: string, attachmentIds: string[], context?: PromptContext, replySource?: 'suggested') => void
  /** Answer the hint: 'confirm' archives as complete, 'reject' drops it. Omitted = read-only hint. */
  onResolveSuspectedComplete?: (decision: Resolution) => void
  /** True while that answer is in flight; both buttons disable. */
  resolvePending?: boolean
  /** Switch to the Changes view; omitted when the session has none to open. */
  onOpenChanges?: () => void
}

interface Props extends Omit<TurnSummaryInputs, 'status' | 'handBackReason' | 'suspectedCompleteReason'> {
  outcome: SummaryOutcome
  files: ChangedFile[]
}

/**
 * The post-turn summary card (D-11): the transcript's tail row once a turn has
 * ended, saying how it ended, which files it changed, and what to do next.
 *
 * It replaced the pinned Your-turn panel and keeps its jobs: a hand-back leads
 * with the session's own sentence (the one needs-you state with no card to
 * answer, #2321); the cached suggestions are one-tap replies; the "Likely
 * complete" hint carries Mark complete / Not done so the decision is taken
 * beside the evidence; and a mid-turn hand_back (#2647) shows as an "Asked you"
 * note while the turn still runs — a reply queues behind it.
 */
export function TurnSummaryCard({
  outcome, files, suggestions, onPrompt, onResolveSuspectedComplete, resolvePending = false, onOpenChanges,
}: Props) {
  const { mode, label, presentation, reason, suspected } = outcome
  const { Icon } = presentation
  const asked = mode === 'asked'
  const handedBack = outcome.status === 'handed_back'
  // Whether a reply is the next move: not on a finished objective.
  const awaitsReply = presentation.tier !== 'complete'
  const shown = files.slice(0, MAX_PATHS)
  const more = files.length - shown.length
  const totals = diffTotals(files)

  return (
    <div className="px-4 py-2">
      <section
        // No aria-live: a virtualized row remounts as it scrolls back into view,
        // and a live region would re-announce it each time.
        aria-label="Turn summary"
        data-testid="turn-summary"
        data-mode={asked ? 'asked' : outcome.status}
        className="rounded-lg border border-edge bg-surface px-3 py-2 text-sm"
      >
        <p className="flex min-w-0 items-start gap-2">
          <Icon size={16} aria-hidden="true" className={cn('mt-0.5 flex-shrink-0', presentation.textClass)} />
          <span className="min-w-0">
            <span className={cn('font-semibold', presentation.textClass)}>{label}</span>
            {asked ? (
              <>
                <span> — {reason}</span>
                {/* The turn is still running, so a reply queues behind it rather than interrupting it. */}
                <span className="text-ink-muted"> (still working — a reply is queued for it)</span>
              </>
            ) : handedBack ? (
              // The session's own sentence reads as prose, not a truncated meta clause.
              <span> — {reason ?? 'the session stopped and is waiting on you.'}</span>
            ) : null}
          </span>
        </p>

        {suspected && (
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 pl-6">
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

        {!asked && files.length > 0 && (
          <div className="mt-2 pl-6">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-muted">
              <span>{files.length === 1 ? '1 file changed' : `${files.length} files changed`}</span>
              {totals && <DiffCounts additions={totals.additions} deletions={totals.deletions} />}
              {onOpenChanges && (
                <button
                  type="button"
                  onClick={onOpenChanges}
                  className={cn(
                    'min-h-target rounded px-1.5 font-medium text-accent hover:bg-surface-raised',
                    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
                  )}
                >
                  Open Changes
                </button>
              )}
            </div>
            <ul aria-label="Changed files" className="mt-1 space-y-0.5">
              {shown.map((f) => (
                <li key={f.path} className="truncate font-mono text-xs" title={f.path}>{f.path}</li>
              ))}
            </ul>
            {more > 0 && <p className="mt-0.5 text-xs text-ink-muted">+{more} more</p>}
          </div>
        )}

        {awaitsReply && (suggestions.length > 0 ? (
          <div className="mt-2 flex flex-wrap gap-2">
            {suggestions.map((s, i) => (
              <button
                key={`${i}-${s.slice(0, 24)}`}
                type="button"
                onClick={() => onPrompt(s, [], undefined, 'suggested')}
                className={cn(
                  // min-h-target: the 44px house tap target — these are one-tap sends on a phone.
                  'min-h-target max-w-full truncate rounded-full border border-edge-strong bg-surface px-3 py-1.5 text-left text-sm',
                  'hover:bg-surface-raised focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
                )}
              >
                {s}
              </button>
            ))}
          </div>
        ) : (
          <p className="mt-1 pl-6 text-xs text-ink-muted">
            {handedBack ? 'Read the last message above, then reply below.' : 'Review the last message above, then reply below.'}
          </p>
        ))}
      </section>
    </div>
  )
}
