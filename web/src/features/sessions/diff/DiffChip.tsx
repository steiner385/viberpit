import { cn } from '@/lib/utils'
import { useSessionView } from '@/features/sessions/sessionView'

interface Props {
  additions: number
  deletions: number
  /** The server capped the diff, so the counts are lower bounds. */
  truncated?: boolean
  className?: string
}

const plural = (n: number, one: string) => `${n} ${one}${n === 1 ? '' : 's'}`

/**
 * The header's `+N −M` chip (spec 2026-11-01 §2): what the session's worktree
 * has changed against its base, and the way into the Changes tab. Nothing at
 * 0/0 — there is no tab to open then either.
 */
export function DiffChip({ additions, deletions, truncated = false, className }: Props) {
  const [, setView] = useSessionView()
  if (additions + deletions <= 0) return null
  const atLeast = truncated ? 'at least ' : ''
  return (
    <button
      type="button"
      onClick={() => setView('changes')}
      aria-label={`${atLeast}${plural(additions, 'addition')}, ${plural(deletions, 'deletion')} — open Changes`}
      title={truncated ? 'Diff truncated — totals are lower bounds' : undefined}
      // The button is the full touch target (`min-h-target`, like every other
      // control in the header); the bordered chip is drawn inside it.
      className={cn(
        'inline-flex min-h-target shrink-0 items-center rounded-md',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
        className,
      )}
    >
      <span className="whitespace-nowrap rounded-md border border-edge px-1.5 py-0.5 font-mono text-xs tabular-nums hover:bg-surface">
        <span className="text-success">+{additions}</span>{' '}
        <span className="text-danger">−{deletions}</span>
      </span>
    </button>
  )
}
