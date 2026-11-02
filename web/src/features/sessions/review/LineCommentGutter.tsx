import type { MouseEvent } from 'react'
import { cn } from '@/lib/utils'

/**
 * The per-line "comment" button in a diff's gutter (D-5). Presentational: the
 * caller owns selection, so a shift-click (`e.shiftKey`) can extend a range.
 * Hidden until its row is hovered or it is focused; always shown on a coarse
 * pointer, where there is no hover. "Its row" is the diff row (`[data-diff-row]`).
 *
 * Ships in the Changes tab's chunk (via `CommentableDiff`), never with the transcript's inline diffs.
 */
export default function LineCommentGutter({ line, selected, onClick }: {
  line: number
  selected: boolean
  onClick: (e: MouseEvent<HTMLButtonElement>) => void
}) {
  return (
    <button
      type="button"
      aria-label={`Comment on line ${line}`}
      aria-pressed={selected}
      onClick={onClick}
      className={cn(
        'inline-flex h-5 w-5 shrink-0 select-none items-center justify-center rounded-sm text-xs leading-none',
        'bg-accent text-accent-ink',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
        selected ? 'opacity-100' : 'opacity-0 [[data-diff-row]:hover_&]:opacity-100 focus-visible:opacity-100 pointer-coarse:opacity-100',
      )}
    >
      <span aria-hidden="true">+</span>
    </button>
  )
}
