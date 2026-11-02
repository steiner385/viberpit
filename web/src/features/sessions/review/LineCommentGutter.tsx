import type { MouseEvent } from 'react'
import { cn } from '@/lib/utils'
import { lineKey } from './diffAnchors'

/**
 * The per-line "comment" button in a diff's gutter (D-5). Presentational: the
 * caller owns selection, so a shift-click (`e.shiftKey`) can extend a range.
 *
 * The visible glyph is a quiet 20px square on every pointer, so a diff row keeps
 * its 20px line height. A mouse reveals it on row hover (`[data-diff-row]`) or
 * focus; a solid fill marks the hovered button and the selected range.
 *
 * Touch (`pointer: coarse` — the ONE media feature gating both): the glyph is
 * always shown, and an invisible `::before` extends the hit area to 44×44
 * (docs/ACCESSIBILITY.md §4) — 12px above and below, 24px rightward over the
 * line numbers (never leftward, where the scrolling frame would clip it).
 * Layering, so the extension claims only empty space: the hit area paints at
 * the positioned (`z-auto`) layer — over the plain rows, numbers and hunk
 * headers (the renderer dims them by colour, not `opacity`, which would be a
 * stacking context over it) — while every glyph and comment slot is
 * `z-raised`, so a neighbour's hit area never steals a tap on either.
 *
 * `side` names the line: `Comment on line N` (new side), `Comment on removed
 * line N` (old side); `noun` replaces "line" (the plan card's `step line`).
 * `data-line` carries `lineKey(side, line)` so a closing flow can return focus
 * here (`review/returnFocus.ts`). Ships in the Changes tab's chunk (via
 * `CommentableDiff`), which also carries the plan card's lazy `PlanStepLines`.
 */
export default function LineCommentGutter({ line, side, noun = 'line', selected, onClick }: {
  line: number
  side: 'old' | 'new'
  noun?: string
  selected: boolean
  onClick: (e: MouseEvent<HTMLButtonElement>) => void
}) {
  return (
    <button
      type="button"
      data-line={lineKey(side, line)}
      aria-label={side === 'old' ? `Comment on removed ${noun} ${line}` : `Comment on ${noun} ${line}`}
      aria-pressed={selected}
      onClick={onClick}
      className={cn(
        'relative inline-flex h-5 w-5 shrink-0 select-none items-center justify-center rounded-sm text-xs leading-none',
        "pointer-coarse:before:absolute pointer-coarse:before:-inset-y-3 pointer-coarse:before:left-0 pointer-coarse:before:-right-6 pointer-coarse:before:content-['']",
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
        selected
          ? 'bg-accent text-accent-ink'
          : cn(
            'bg-accent-wash text-accent hover:bg-accent hover:text-accent-ink',
            'opacity-0 [[data-diff-row]:hover_&]:opacity-100 focus-visible:opacity-100 pointer-coarse:opacity-100',
          ),
      )}
    >
      <span aria-hidden="true" className="relative z-raised inline-flex h-full w-full items-center justify-center">+</span>
    </button>
  )
}

/** An empty gutter cell, as wide as the button. */
export function GutterSpacer() {
  return <span aria-hidden="true" className="w-5 shrink-0" />
}
