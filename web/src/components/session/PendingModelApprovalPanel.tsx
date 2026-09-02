/**
 * PendingModelApprovalPanel — sticky panel above the composer asking the user to
 * approve or deny a gated model switch.
 *
 * Mirrors PendingPermissionPanel in structure: flex-shrink-0, border-t,
 * role="region" aria-live="polite". Rendered by SessionBody when a running
 * session has `pendingModel` set (mid-session gate), and by the dashboard for
 * launch-time `pending_approval` sessions.
 *
 * Also shows WHY the request was made (`reason`), WHO made it (`requestedBy` —
 * "session:self" means the session asked for itself, not a human), and lets the
 * approver pick a different model than the one requested ("Approve as…") before
 * approving.
 */

import { useState } from 'react'
import { cn } from '@/lib/utils'
import { tierLabel } from '@/session/modelName'
import { useModels } from '@/api/hooks'

interface Props {
  /** The model alias or full id requiring approval (e.g. "fable", "claude-opus-4-8"). */
  model: string | null | undefined
  /** The requester's stated reason, when there is one. */
  reason?: string | null
  /** Identity that requested it; "session:self" means the session asked for itself. */
  requestedBy?: string | null
  /** Second arg is the model to approve AS — omitted for a deny. */
  onDecision: (decision: 'approve' | 'deny', model?: string) => void
}

export function PendingModelApprovalPanel({ model, reason, requestedBy, onDecision }: Props) {
  const label = tierLabel(model)
  const { data: models } = useModels()
  // Default the picker to the requested model so a plain Approve is unchanged.
  const [choice, setChoice] = useState<string>(model ?? '')
  // The panel stays mounted while a hold is pending, and a standing hold can be
  // overwritten by a DIFFERENT parked model (e.g. fable parks, goes unanswered,
  // then opus parks) — the dedupe upstream only short-circuits on an IDENTICAL
  // model. Without this, `choice` would keep reading the stale model while the
  // headline updates, so Approve would silently grant the wrong (often costlier)
  // model. Re-sync `choice` to a new `model` prop during render (no setState-in-
  // effect, matching Dashboard.tsx's repo/branch pattern) rather than in a
  // useEffect, which would flash the stale value for one paint.
  const [prevModel, setPrevModel] = useState(model)
  if (model !== prevModel) {
    setPrevModel(model)
    setChoice(model ?? '')
  }
  const bySession = requestedBy === 'session:self'
  // A pinned id (e.g. "claude-opus-5") is valid input but is not in the alias catalog.
  // Prepend it so the default selection is always a real option.
  const options = models ?? []
  const allOptions = !model || options.some((m) => m.id === model)
    ? options
    : [{ id: model, label }, ...options]

  return (
    <div
      role="region"
      aria-label="Pending model approval"
      aria-live="polite"
      className={cn(
        'flex-shrink-0 border-t border-violet-200 dark:border-violet-800',
        'bg-violet-50/60 dark:bg-violet-950/20 px-3 py-2',
        'flex flex-wrap items-center gap-3',
      )}
      data-testid="pending-model-approval-panel"
    >
      <span className="text-sm flex-1 min-w-[12rem]">
        <strong>{label}</strong> requires approval before this session can use it.
        {bySession && (
          <em className="not-italic text-gray-600 dark:text-gray-400"> — the session requested this.</em>
        )}
        {reason && (
          <span className="block text-xs text-gray-600 dark:text-gray-400 mt-0.5">
            &ldquo;{reason}&rdquo;
          </span>
        )}
      </span>
      <label className="flex shrink-0 items-center gap-1 text-sm">
        <span className="text-gray-600 dark:text-gray-400">Approve as</span>
        <select
          aria-label="Approve as"
          value={choice}
          onChange={(e) => setChoice(e.target.value)}
          className={cn(
            'rounded border border-gray-300 dark:border-gray-600 px-2 py-1 text-sm',
            'bg-white dark:bg-gray-900',
          )}
        >
          {allOptions.map((m) => (
            // No text child: the `label` attribute renders the same visible text
            // (real browsers honor it) without adding a second DOM text node that
            // duplicates the summary sentence's model name — see the panel test's
            // "shows the requested model label" case, which queries for that text
            // uniquely.
            <option key={m.id} value={m.id} label={m.label} />
          ))}
        </select>
      </label>
      <div className="flex shrink-0 gap-2">
        <button
          type="button"
          onClick={() => onDecision('approve', choice || undefined)}
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
          onClick={() => onDecision('deny', undefined)}
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
