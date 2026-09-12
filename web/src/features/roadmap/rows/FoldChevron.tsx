// The fold/unfold chevron a program row (`InitiativeCard`) and a tree row
// (`TreeRowCard`) both carry when they have children — same button, same
// roving tab stop, same `aria-label` wording, so one file is the only place
// its markup can drift between the two.
import { ChevronDown, ChevronRight } from 'lucide-react'
import { cn } from '@/lib/utils'

export interface FoldChevronProps {
  expanded: boolean
  /** The row's own title, folded into `aria-label` ("Collapse <label>" / "Expand <label>"). */
  label: string
  /** Roving tabindex: only the tree's one focusable row keeps this a tab stop. */
  focusable: boolean
  onToggle: () => void
}

export function FoldChevron({ expanded, label, focusable, onToggle }: FoldChevronProps) {
  return (
    <button
      type="button"
      // Same roving tab stop as the open control beside it: one Tab enters
      // the tree, `←`/`→` fold and unfold from there.
      tabIndex={focusable ? 0 : -1}
      aria-label={`${expanded ? 'Collapse' : 'Expand'} ${label}`}
      onClick={(e) => { e.stopPropagation(); onToggle() }}
      className={cn(
        'flex size-6 shrink-0 items-center justify-center rounded-sm text-ink-muted',
        'hover:bg-surface-raised focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
      )}
    >
      {expanded
        ? <ChevronDown size={14} aria-hidden="true" />
        : <ChevronRight size={14} aria-hidden="true" />}
    </button>
  )
}
