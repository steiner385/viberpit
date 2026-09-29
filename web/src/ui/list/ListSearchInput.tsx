// The one search box a list column mounts: the input plus its clear
// affordance. Extracted verbatim from `SessionListControls`, which had the only
// copy — the roadmap's Map scope then hand-rolled a second one that looked and
// behaved differently (no clear button, its own sizing) right beside the
// Initiatives scope's. Same reasoning as `ScopeBody`: one place a change to the
// control has to land.
import { X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { listControlClass } from './listControlStyles'

export interface ListSearchInputProps {
  value: string
  onChange: (next: string) => void
  placeholder?: string
  /** Accessible name — every list names what it searches ("Search sessions"). */
  ariaLabel?: string
  /** Width (and anything else) for the wrapper; the input fills it. */
  className?: string
}

export function ListSearchInput({
  value, onChange, placeholder = 'Search…', ariaLabel = 'Search', className,
}: ListSearchInputProps) {
  return (
    // A `search` landmark so a screen-reader user can jump to it. Deliberately
    // UNNAMED: the input inside already carries "Search sessions", and a second
    // element with the same label makes every `getByLabelText` ambiguous — one
    // list column is on a page at a time, so the bare landmark is unambiguous.
    <div role="search" className={cn('relative', className)}>
      <input
        type="search"
        aria-label={ariaLabel}
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={cn(listControlClass, 'w-full pr-7 [&::-webkit-search-cancel-button]:hidden')}
      />
      {value !== '' && (
        <button
          type="button"
          aria-label="Clear search"
          onClick={() => onChange('')}
          className={cn(
            'absolute right-0 top-1/2 -translate-y-1/2 flex items-center justify-center',
            // Same touch/hover split as `listControlClass` — a 14px glyph stays
            // visually unchanged; the invisible hit area grows to 44px square
            // on a coarse pointer and shrinks back off for a desktop mouse.
            'min-h-11 min-w-11 [@media(hover:hover)]:min-h-0 [@media(hover:hover)]:min-w-0 [@media(hover:hover)]:p-0.5',
            'text-ink-muted hover:text-ink',
          )}
        >
          <X size={14} aria-hidden="true" />
        </button>
      )}
    </div>
  )
}
