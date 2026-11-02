/**
 * Review comments (D-5): the model the Changes pane's line comments and the
 * plan card's per-step comments share, plus the two composers that turn them
 * into the single prompt / deny message sent to Claude.
 */

/** Inclusive line range on one side of a diff. `path` is null for plan comments. */
export type CommentAnchor = { path: string | null; side: 'new' | 'old'; start: number; end: number }

/** `quote` is captured at creation so a later diff refresh never loses what was commented on. */
export type LineComment = { id: string; anchor: CommentAnchor; quote: string[]; body: string }

/** The server's deny-message cap; a longer plan feedback disables Send. */
export const PLAN_FEEDBACK_MAX = 4000

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

export function composePlanFeedback(overall: string, stepComments: readonly LineComment[]): string {
  const head = overall.trim()
  if (stepComments.length === 0) return head
  const steps = stepComments.map((c) => `- line ${c.anchor.start}: "${c.quote.join(' ')}" — ${c.body.trim()}`)
  return [...(head ? [head, ''] : []), 'On the plan:', ...steps].join('\n')
}
