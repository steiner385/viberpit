import { cn } from '@/lib/utils'

interface Props {
  /** Pre-restart status from the server (Session.interruptedFromStatus). */
  interruptedFromStatus?: string | null
  onAskAgain: () => void
}

const COPY: Record<string, string> = {
  waiting_input:
    'A daemon restart interrupted a permission request, so the prompt is gone and the '
    + 'turn it belonged to has ended. Nothing was approved.',
  ready_review:
    'A daemon restart interrupted a plan approval, so the prompt is gone and the turn '
    + 'it belonged to has ended. Nothing was approved.',
}

/**
 * A permission or plan approval whose waiter died with the daemon.
 *
 * Unlike a question — which is a real AskUserQuestion tool_use and can be re-offered
 * from the transcript — a permission leaves NO transcript record (can_use_tool is a
 * live callback) and a plan replays only as a raw ExitPlanMode tool card. Neither can
 * be re-presented as a choice, so the honest affordance is to say the prompt was lost
 * and offer to have the assistant ask again.
 */
export function InterruptedAskNotice({ interruptedFromStatus, onAskAgain }: Props) {
  const copy = COPY[interruptedFromStatus ?? '']
  if (!copy) return null
  return (
    <div
      data-testid="interrupted-ask-notice"
      role="status"
      className={cn(
        'flex-shrink-0 border-t border-amber-200 dark:border-amber-800',
        'bg-amber-50/60 dark:bg-amber-950/20 px-3 py-2 text-xs',
        'text-amber-900 dark:text-amber-200 flex items-center gap-3',
      )}
    >
      <span className="flex-1">{copy}</span>
      <button
        type="button"
        onClick={onAskAgain}
        className={cn(
          'flex-shrink-0 rounded border border-amber-400 dark:border-amber-600',
          'px-2 py-1 font-medium hover:bg-amber-100 dark:hover:bg-amber-900/40',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500',
        )}
      >
        Ask again
      </button>
    </div>
  )
}
