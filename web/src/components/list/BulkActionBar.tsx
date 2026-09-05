//
// The bulk-action bar's SHELL — the labelled region, the count text and Clear.
// The actions are `children`: the live list offers Archive / Archive-done, the
// archived list Unarchive / Delete, and the two share none of them. Modelling
// both sets as props would be a union of six optionals with four always unset.
import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { Button } from '@/ui/Button'

interface BulkActionBarProps {
  /** The count phrase, e.g. "3 selected" or "Select sessions to archive". */
  label: string
  /** Text of the dismiss control. Live says "Done", archived says "Clear". */
  clearLabel?: string
  onClear: () => void
  children?: ReactNode
}

export function BulkActionBar({ label, clearLabel = 'Clear', onClear, children }: BulkActionBarProps) {
  return (
    <div
      role="region"
      aria-label="Bulk actions"
      className={cn(
        'flex flex-wrap items-center gap-2 rounded-lg px-3 py-2',
        'bg-gray-50 dark:bg-gray-800/60 border border-gray-200 dark:border-gray-700',
      )}
    >
      <span className="text-sm font-medium text-gray-700 dark:text-gray-300">{label}</span>
      <div className="flex items-center gap-2 ml-auto flex-wrap">
        {children}
        <Button type="button" variant="secondary" onClick={onClear} className="min-h-[44px]">
          {clearLabel}
        </Button>
      </div>
    </div>
  )
}
