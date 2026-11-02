import type { MouseEvent } from 'react'
import { cn } from '@/lib/utils'

/**
 * The per-line "comment" button in a diff's gutter (D-5). Presentational: the
 * caller owns selection, so a shift-click (`e.shiftKey`) can extend a range.
 * Hidden until its row (`[data-diff-row]`) is hovered or it is focused; always
 * shown on a coarse pointer, where there is no hover.
 *
 * Touch target: a 44px floor on a coarse pointer, lifted where `hover: hover`
 * matches so a mouse keeps the dense 20px gutter (docs/ACCESSIBILITY.md §4,
 * `CardMenu`'s `actionBtn` pattern). An empty gutter cell takes the same width
 * (`GutterSpacer`) so split columns stay aligned.
 *
 * `side` names the line: `Comment on line N` (new side), `Comment on removed
 * line N` (old side). Ships in the Changes tab's chunk (via `CommentableDiff`).
 */
export default function LineCommentGutter({ line, side, selected, onClick }: {
  line: number
  side: 'old' | 'new'
  selected: boolean
  onClick: (e: MouseEvent<HTMLButtonElement>) => void
}) {
  return (
    <button
      type="button"
      aria-label={side === 'old' ? `Comment on removed line ${line}` : `Comment on line ${line}`}
      aria-pressed={selected}
      onClick={onClick}
      className={cn(
        'inline-flex h-5 w-5 shrink-0 select-none items-center justify-center rounded-sm text-xs leading-none',
        'min-h-11 min-w-11 [@media(hover:hover)]:min-h-0 [@media(hover:hover)]:min-w-0',
        'bg-accent text-accent-ink',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
        selected ? 'opacity-100' : 'opacity-0 [[data-diff-row]:hover_&]:opacity-100 focus-visible:opacity-100 pointer-coarse:opacity-100',
      )}
    >
      <span aria-hidden="true">+</span>
    </button>
  )
}

/** An empty gutter cell, as wide as the button on every pointer. */
export function GutterSpacer() {
  return <span aria-hidden="true" className="w-5 shrink-0 min-w-11 [@media(hover:hover)]:min-w-0" />
}
