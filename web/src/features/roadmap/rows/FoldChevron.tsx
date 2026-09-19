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
  onToggle: () => void
}

export function FoldChevron({ expanded, label, onToggle }: FoldChevronProps) {
  return (
    <button
      type="button"
      // NEVER a tab stop. The WAI-ARIA tree pattern is ONE tab stop for the whole
      // tree, and this button sits on the same row as that tab stop — so making
      // it roving too (which it was) put a SECOND stop on the roving row: Tab
      // into the tree landed on the chevron, Tab again on the row's open control,
      // and only the third Tab left. `←`/`→` already fold and unfold from the row
      // (`useTreeKeys`), which is how a tree is meant to be folded; the chevron is
      // the POINTER affordance for the same thing.
      tabIndex={-1}
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
