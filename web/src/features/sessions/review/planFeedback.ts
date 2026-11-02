import type { LineComment } from './reviewComments'

/**
 * The plan card's "Keep planning" feedback (D-5): the overall text plus the
 * per-step comments, composed into the one deny message. Its own module, apart
 * from `reviewComments.ts`, because the plan card is first-paint: importing
 * this keeps only these few lines in the entry chunk, where importing
 * `reviewComments` would preload the module the Changes tab's chunk shares.
 */

/** The server's deny-message cap (`MESSAGE_TOO_LONG` past it); a longer feedback disables Send. */
export const PLAN_FEEDBACK_MAX = 4000

export function composePlanFeedback(overall: string, stepComments: readonly LineComment[]): string {
  const head = overall.trim()
  if (stepComments.length === 0) return head
  const steps = stepComments.map((c) => `- line ${c.anchor.start}: "${c.quote.join(' ')}" — ${c.body.trim()}`)
  return [...(head ? [head, ''] : []), 'On the plan:', ...steps].join('\n')
}
