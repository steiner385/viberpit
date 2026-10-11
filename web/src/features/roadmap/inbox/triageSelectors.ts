/**
 * Pure selectors over the triage queue.
 *
 * `GET /api/pm/triage` is WIDENED for exactly one surface: besides the pending
 * proposals it carries the PM Triage loop's fresh auto-accepts for their undo
 * window (spec 2026-09-12 product automation policy §8), so the inbox can render
 * them with an "auto-accepted · undo" chip. Every other reader of that list means
 * "awaiting a human decision" — the nav badge, the Inbox scope's count, the
 * selection the bulk bar may act on — and counting an already-decided row there
 * says "6 need you" about 5.
 *
 * So "a proposal is still pending" gets ONE definition, here, and every consumer
 * routes through it — no call site compares `status` itself. An inline copy at
 * each site is free to drift the moment the wire gains a fourth status, and the
 * surfaces then disagree about the size of one queue.
 *
 * Since spec 2026-09-18 §6 that queue has TWO card kinds: classification
 * proposals and decision proposals, listed together. These selectors are
 * generic over the row rather than typed to `TriageProposal` for exactly that
 * reason — "awaiting a human" stays one predicate over one queue, the nav badge
 * counts both kinds through it, and neither kind gets a parallel `isPending`.
 *
 * Scope note: `InitiativePage`'s `proposal?.status === 'pending'` is deliberately
 * NOT this — that is a `MaterializationProposal`, a different domain that merely
 * shares the word.
 */

import { isTriageProposal, type InboxProposal, type TriageProposal } from '@/api/triage'
import { isValidTimestamp } from '@/lib/relativeTime'

/** The only thing either kind of row must carry to be asked this question.
 *  `itemState` is the classification row's work-item state (decision rows have
 *  none, which reads as "still open"). */
interface HasStatus { status: string; itemState?: string | null }

/** Work-item states that leave nothing to triage. */
const GONE_ITEM_STATES: ReadonlySet<string> = new Set(['closed', 'merged'])

/**
 * Is this row's work item already closed or merged? The server's tick resolves
 * such a proposal eventually; until it does, the inbox must not ask a human to
 * decide it — so it neither counts as pending nor stays in the list.
 */
export function isItemGone<T extends HasStatus>(p: T | null | undefined): boolean {
  return p?.itemState != null && GONE_ITEM_STATES.has(p.itemState)
}

/** Is this one proposal still awaiting a human? The ONE `'pending'` literal.
 *  Takes null/undefined because a detail pane renders before its row arrives.
 *  A row whose work item is closed/merged is NOT pending (`isItemGone`). */
export function isPending<T extends HasStatus>(p: T | null | undefined): p is T {
  return p?.status === 'pending' && !isItemGone(p)
}

/**
 * The rows still awaiting a human decision, in the order they came in.
 *
 * `readonly` in: the inbox's own list is a `readonly` prop, the nav badge's is
 * mutable, and a mutable parameter would shut the inbox out of its own
 * selector — the one call site this exists for.
 */
export function pendingProposals<T extends HasStatus>(rows: readonly T[]): T[] {
  return rows.filter((p): p is T => isPending(p))
}

/** Past this, the oldest pending proposal is overdue and the header warns. */
export const INBOX_STALE_AFTER_MS = 2 * 86_400_000

export interface InboxAging {
  /** How many rows await a human (`pendingProposals`). */
  pending: number
  /** `createdAt` of the oldest pending row with a usable timestamp, or null. */
  oldestCreatedAt: string | null
  /** `now − oldestCreatedAt`, clamped at 0; null when there is no oldest. */
  oldestAgeMs: number | null
  /** The oldest has waited longer than `INBOX_STALE_AFTER_MS`. */
  stale: boolean
}

/**
 * The queue's size and how long its oldest row has waited — the inbox header's
 * "142 pending · oldest 6d ago". Over BOTH card kinds, through `isPending`, so
 * it can never disagree with the nav badge about the count.
 */
export function inboxAging<T extends HasStatus & { createdAt: string | null }>(
  rows: readonly T[], now: number,
): InboxAging {
  const pending = pendingProposals(rows)
  let oldest: string | null = null
  let oldestMs = Infinity
  for (const p of pending) {
    if (!isValidTimestamp(p.createdAt)) continue
    const t = new Date(p.createdAt).getTime()
    if (t < oldestMs) { oldestMs = t; oldest = p.createdAt }
  }
  const oldestAgeMs = oldest === null ? null : Math.max(0, now - oldestMs)
  return {
    pending: pending.length,
    oldestCreatedAt: oldest,
    oldestAgeMs,
    stale: oldestAgeMs !== null && oldestAgeMs > INBOX_STALE_AFTER_MS,
  }
}

/**
 * The rows "Accept all high-confidence" may sweep: pending CLASSIFICATION
 * proposals the classifier rated `high` AND homed on a real capability. A
 * no-fit proposal has no `proposedFeatureId` — accepting it as proposed homes
 * the item nowhere — and a decision cannot be bulk-accepted at all.
 */
export function highConfidenceAcceptable(rows: readonly InboxProposal[]): TriageProposal[] {
  return pendingProposals(rows)
    .filter(isTriageProposal)
    .filter((p) => p.confidenceLevel === 'high' && p.proposedFeatureId != null)
}
