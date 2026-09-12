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
      className={cn(
        'flex-shrink-0 border-t border-warning',
        'bg-warning-wash/60 px-3 py-2 text-xs',
        'text-warning flex items-center gap-3',
      )}
    >
      {/* The live region is the COPY, not the wrapper: role="status" on the outer
          div makes it announce the "Ask again" button label too. */}
      <span role="status" className="flex-1">{copy}</span>
      <button
        type="button"
        onClick={onAskAgain}
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
