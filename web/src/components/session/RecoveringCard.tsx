import { cn } from '@/lib/utils'

interface Props {
  message: string
}

/**
 * Inline transcript card for a self-healing dropped-turn auto-resend. Deliberately
 * muted (no red, no alert role) — the turn produced no output and is being resent;
 * it is recovering, not failed. Contrast ErrorCard, the red give-up surface used
 * only when auto-recovery is exhausted.
 */
export function RecoveringCard({ message }: Props) {
  return (
    <div
      className={cn(
        'my-1 rounded-md px-3 py-2 text-sm',
        'bg-amber-50 dark:bg-amber-950/30 text-amber-800 dark:text-amber-300/90',
        'border border-amber-200/70 dark:border-amber-900/60',
      )}
    >
      <span className="font-medium">Recovering</span>
      <div className="mt-0.5 whitespace-pre-wrap break-words">{message}</div>
    </div>
  )
}
