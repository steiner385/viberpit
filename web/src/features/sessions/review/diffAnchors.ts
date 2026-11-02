/**
 * How a review comment's anchor maps onto a diff (D-5): which lines exist on
 * which side, a range's quoted text, and whether a comment still anchors. One
 * module so the Changes tab (what it marks `(outdated)` in the prompt) and
 * `CommentableDiff` (what it renders as Outdated) can never disagree.
 */
import type { DiffHunk, DiffLine } from '../diff/types'
import type { CommentAnchor, LineComment } from './reviewComments'

export type Side = CommentAnchor['side']

/** Whether `line` exists on `side`: the new side has context + adds, the old side context + dels. */
export function onSide(line: DiffLine, side: Side): boolean {
  return side === 'old' ? line.kind !== 'add' : line.kind !== 'del'
}

export function lineNo(line: DiffLine, side: Side): number | null {
  return side === 'old' ? line.oldNo : line.newNo
}

/** The one `side:number` key for a line on a side — how comments, rows and gutter buttons find each other. */
export function lineKey(side: Side, n: number | null): string {
  return `${side}:${n}`
}

/** The `side:number` keys a line answers to — both sides for a context line. */
export function lineKeys(line: DiffLine): string[] {
  return (['old', 'new'] as const).filter((s) => onSide(line, s)).map((s) => lineKey(s, lineNo(line, s)))
}

/** The text of `side`'s lines `start..end` in one hunk. */
export function quoteOf(hunk: DiffHunk, side: Side, start: number, end: number): string[] {
  return hunk.lines
    .filter((l) => {
      const n = onSide(l, side) ? lineNo(l, side) : null
      return n != null && n >= start && n <= end
    })
    .map((l) => l.text)
}

/** True when some hunk still has the anchor's whole range, reading exactly as `quote`. */
export function anchorMatches(hunks: readonly DiffHunk[], anchor: CommentAnchor, quote: readonly string[]): boolean {
  const { side, start, end } = anchor
  return hunks.some((h) => {
    const now = quoteOf(h, side, start, end)
    return now.length === end - start + 1 && now.length === quote.length && now.every((t, k) => t === quote[k])
  })
}

/** Ids of the comments that no longer anchor in `hunksByPath` (a file gone from the diff included). */
export function staleCommentIds(
  comments: readonly LineComment[],
  hunksByPath: ReadonlyMap<string, readonly DiffHunk[]>,
): Set<string> {
  return new Set(comments
    .filter((c) => !anchorMatches(hunksByPath.get(c.anchor.path ?? '') ?? [], c.anchor, c.quote))
    .map((c) => c.id))
}
