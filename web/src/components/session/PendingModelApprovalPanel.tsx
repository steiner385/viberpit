/**
 * PendingModelApprovalPanel — sticky panel above the composer asking the user to
 * approve or deny a gated model switch.
 *
 * Mirrors PendingPermissionPanel in structure: flex-shrink-0, border-t,
 * role="region" aria-live="polite". Rendered by SessionBody when a running
 * session has `pendingModel` set (mid-session gate), and by the dashboard for
 * launch-time `pending_approval` sessions.
 */

import { cn } from '@/lib/utils'
import { modelName } from '@/session/modelName'

// Short aliases like "fable" are not recognised by modelName (which expects the
// full `claude-<tier>-<maj>` id). Capitalise them via the same TIERS map so
// "fable" renders as "Fable" rather than verbatim lowercase.
const TIER_LABELS: Record<string, string> = {
  opus: 'Opus',
  sonnet: 'Sonnet',
  haiku: 'Haiku',
  fable: 'Fable',
}

function displayLabel(model: string | null | undefined): string {
  if (!model) return 'this model'
  const full = modelName(model)
  if (full && full !== model) return full          // matched the full-id pattern
  return TIER_LABELS[model.toLowerCase()] ?? model // short alias or verbatim
}

interface Props {
  /** The model alias or full id requiring approval (e.g. "fable", "claude-opus-4-8"). */
  model: string | null | undefined
  onDecision: (decision: 'approve' | 'deny') => void
}

export function PendingModelApprovalPanel({ model, onDecision }: Props) {
  const label = displayLabel(model)
  return (
    <div
      role="region"
      aria-label="Pending model approval"
      aria-live="polite"
      className={cn(
        'flex-shrink-0 border-t border-violet-200 dark:border-violet-800',
        'bg-violet-50/60 dark:bg-violet-950/20 px-3 py-2',
        'flex items-center gap-3',
      )}
      data-testid="pending-model-approval-panel"
    >
      <span className="text-sm flex-1 min-w-0">
        <strong>{label}</strong> requires approval before this session can use it.
      </span>
      <div className="flex shrink-0 gap-2">
        <button
          type="button"
          onClick={() => onDecision('approve')}
          className={cn(
            'rounded px-3 py-1 text-sm font-medium',
            'bg-violet-600 text-white hover:bg-violet-700',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500',
          )}
        >
          Approve
        </button>
        <button
          type="button"
          onClick={() => onDecision('deny')}
          className={cn(
            'rounded px-3 py-1 text-sm font-medium',
            'border border-gray-300 dark:border-gray-600',
            'text-gray-700 dark:text-gray-300',
            'hover:bg-gray-100 dark:hover:bg-gray-800',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500',
          )}
        >
          Deny
        </button>
      </div>
    </div>
  )
}
