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
    <div className={cn('relative', className)}>
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
          className="absolute right-1 top-1/2 -translate-y-1/2 p-0.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
        >
          <X size={14} aria-hidden="true" />
        </button>
      )}
    </div>
  )
}
