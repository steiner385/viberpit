//
// "The list is empty because you narrowed it" — as distinct from "you have
// none", which is `EmptyState`. Conflating the two is how a filtered list came
// to invite the user to create their first session.
//
// `role="status"` so the reason is announced rather than silently replacing the
// rows; the reset is the caller's, because only the caller knows what it has to
// undo (a search, a search AND filters, a scope).
import { cn } from '@/lib/utils'

interface NoMatchStateProps {
  message: string
  actionLabel: string
  onClear: () => void
}

export function NoMatchState({ message, actionLabel, onClear }: NoMatchStateProps) {
  return (
    <div
      className="rounded-lg border border-dashed border-edge p-6 text-center"
      role="status"
    >
      <p className="text-sm text-ink-muted">{message}</p>
      <button
        type="button"
        onClick={onClear}
        className={cn(
          'mt-2 text-sm font-medium text-accent hover:underline',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded',
        )}
      >
        {actionLabel}
      </button>
    </div>
  )
}
