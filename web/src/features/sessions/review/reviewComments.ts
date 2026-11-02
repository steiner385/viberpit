/**
 * Review comments (D-5): the model the Changes pane's line comments and the
 * plan card's per-step comments share, plus `composeReviewPrompt`, which turns
 * them into the single prompt sent to Claude. The plan card's deny-message
 * composer lives in `planFeedback.ts` (it ships in the entry chunk).
 */

/** Inclusive line range on one side of a diff. `path` is null for plan comments. */
export type CommentAnchor = { path: string | null; side: 'new' | 'old'; start: number; end: number }

/** `quote` is captured at creation so a later diff refresh never loses what was commented on. */
export type LineComment = { id: string; anchor: CommentAnchor; quote: string[]; body: string }

const QUOTE_MAX_LINES = 5

export function newCommentId(): string {
  return crypto.randomUUID()
}

const SIDE_ORDER = { new: 0, old: 1 } as const

/** Path, then `new` before `old`, then start line. Returns a new array. */
export function sortComments(comments: readonly LineComment[]): LineComment[] {
  return [...comments].sort((a, b) => {
    const pa = a.anchor.path ?? ''
    const pb = b.anchor.path ?? ''
    if (pa !== pb) return pa < pb ? -1 : 1
    if (a.anchor.side !== b.anchor.side) return SIDE_ORDER[a.anchor.side] - SIDE_ORDER[b.anchor.side]
    return a.anchor.start - b.anchor.start
  })
}

function quoteLines(quote: readonly string[]): string[] {
  const shown = quote.slice(0, QUOTE_MAX_LINES).map((l) => `   > ${l}`)
  if (quote.length > QUOTE_MAX_LINES) shown.push('   > …')
  return shown
}

/** `stale` holds the ids of comments whose anchor no longer matches the current diff — marked ` (outdated)`. */
export function composeReviewPrompt(comments: readonly LineComment[], stale: ReadonlySet<string> = new Set()): string {
  const blocks = sortComments(comments).map((c, i) => {
    const { path, side, start, end } = c.anchor
    const range = end > start ? `${start}–${end}` : `${start}`
    const head = `${i + 1}. \`${path}\` line ${range}${side === 'old' ? ' (removed)' : ''}${stale.has(c.id) ? ' (outdated)' : ''}:`
    return [head, ...quoteLines(c.quote), `   ${c.body.trim()}`].join('\n')
  })
  return [
    `Review comments on the current changes (${comments.length}):`,
    '',
    blocks.join('\n\n'),
    '',
    'Address each comment, then summarize what you changed.',
  ].join('\n')
}
